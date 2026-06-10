"""Pipeline controller: a job-queue layer on top of LosslessCutService.

Responsibilities:
  - Start/stop the LosslessCut subprocess (delegated to LosslessCutService).
  - Queue edit jobs (open file -> [import EDL] -> export -> close).
  - Track per-job state and surface step-level errors.

EDL delivery strategies
-----------------------
``llc_sidecar`` (default, headless-safe):
    The caller writes a ``<basename>-proj.llc`` project file next to the
    video before the job runs. When LosslessCut opens the media file it
    auto-loads the sidecar project (see App.tsx ``tryFindAndLoadProjectFile``),
    so no import action is needed. This works against unmodified upstream.

``import_action``:
    Dispatches ``importEdlFile`` over the HTTP API after opening the file.
    NOTE: upstream's importEdlFile action opens a file-picker dialog (it takes
    an EdlImportType, not a path), so this strategy only works against a
    patched build that accepts a direct path. Kept as an explicit opt-in seam.
"""

import logging
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional

from backend.domain.services.losslesscut_service import LosslessCutService

logger = logging.getLogger(__name__)


class JobStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class EditJob:
    """A single open -> import -> export unit of work."""

    video_path: str
    edl_path: Optional[str] = None
    job_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    status: JobStatus = JobStatus.PENDING
    error: Optional[str] = None
    failed_step: Optional[str] = None
    steps_completed: list = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    finished_at: Optional[float] = None

    @property
    def is_terminal(self) -> bool:
        return self.status in (JobStatus.COMPLETED, JobStatus.FAILED)


class PipelineController:
    """Queue and execute edit jobs against a LosslessCut instance.

    The controller is synchronous and single-instance by design: LosslessCut
    exposes one HTTP API and edits one "current file" at a time, so jobs are
    processed strictly in FIFO order.
    """

    VALID_STRATEGIES = ("llc_sidecar", "import_action")

    def __init__(
        self,
        service: Optional[LosslessCutService] = None,
        edl_strategy: str = "llc_sidecar",
        open_settle_seconds: float = 5.0,
        close_after_export: bool = True,
        sleep: Callable[[float], None] = time.sleep,
    ):
        if edl_strategy not in self.VALID_STRATEGIES:
            raise ValueError(f"edl_strategy must be one of {self.VALID_STRATEGIES}, got {edl_strategy!r}")
        self.service = service or LosslessCutService()
        self.edl_strategy = edl_strategy
        self.open_settle_seconds = open_settle_seconds
        self.close_after_export = close_after_export
        self._sleep = sleep
        self._queue: deque[EditJob] = deque()
        self._jobs: dict[str, EditJob] = {}

    # ── Lifecycle ─────────────────────────────────────────────────

    def ensure_running(self) -> bool:
        """Start LosslessCut if it is not already running."""
        if self.service.is_running():
            return True
        logger.info("LosslessCut not running — starting it")
        return self.service.start()

    def shutdown(self) -> bool:
        """Stop the LosslessCut subprocess."""
        return self.service.stop()

    # ── Queue management ──────────────────────────────────────────

    def submit_job(self, video_path: str, edl_path: Optional[str] = None) -> EditJob:
        """Queue an edit job. Returns the (pending) job record."""
        job = EditJob(video_path=video_path, edl_path=edl_path)
        self._queue.append(job)
        self._jobs[job.job_id] = job
        logger.info("Queued job %s for %s", job.job_id, video_path)
        return job

    def get_job(self, job_id: str) -> Optional[EditJob]:
        return self._jobs.get(job_id)

    @property
    def jobs(self) -> list[EditJob]:
        return list(self._jobs.values())

    @property
    def pending_count(self) -> int:
        return len(self._queue)

    # ── Execution ─────────────────────────────────────────────────

    def run_next(self) -> Optional[EditJob]:
        """Pop and execute the next queued job. Returns None if queue empty."""
        if not self._queue:
            return None
        job = self._queue.popleft()
        return self.run_job(job)

    def run_all(self) -> list[EditJob]:
        """Drain the queue, executing every pending job in order."""
        done = []
        while self._queue:
            done.append(self.run_next())
        return done

    def run_job(self, job: EditJob) -> EditJob:
        """Execute a job's step sequence, recording state transitions."""
        job.status = JobStatus.RUNNING
        job.started_at = time.time()
        logger.info("Running job %s (%s)", job.job_id, job.video_path)

        try:
            if not self.ensure_running():
                return self._fail(job, "start", "LosslessCut failed to start")

            # 1. Open the video. With the llc_sidecar strategy, a pre-written
            #    <basename>-proj.llc next to the video is auto-loaded here.
            result = self.service.open_files([job.video_path])
            if result.get("status") != "ok":
                return self._fail(job, "open", result.get("message", "open failed"))
            job.steps_completed.append("open")

            # Give the renderer time to load the file (and sidecar project)
            # before issuing edit/export actions — see docs/api.md batch example.
            if self.open_settle_seconds > 0:
                self._sleep(self.open_settle_seconds)

            # 2. Deliver the EDL via the explicit import action if requested.
            if job.edl_path and self.edl_strategy == "import_action":
                result = self.service.import_edl(job.edl_path)
                if result.get("status") != "ok":
                    return self._fail(job, "import_edl", result.get("message", "import failed"))
                job.steps_completed.append("import_edl")

            # 3. Export the segments losslessly.
            result = self.service.export()
            if result.get("status") != "ok":
                return self._fail(job, "export", result.get("message", "export failed"))
            job.steps_completed.append("export")

            # 4. Close so the next job starts from a clean slate.
            if self.close_after_export:
                result = self.service.close_file()
                if result.get("status") != "ok":
                    # Non-fatal: the export already succeeded.
                    logger.warning(
                        "Job %s: close_file failed (%s) — continuing",
                        job.job_id,
                        result.get("message"),
                    )
                else:
                    job.steps_completed.append("close")

            job.status = JobStatus.COMPLETED
            job.finished_at = time.time()
            logger.info("Job %s completed", job.job_id)
            return job
        except Exception as exc:  # defensive: never leave a job RUNNING
            logger.exception("Job %s crashed", job.job_id)
            return self._fail(job, "unexpected", str(exc))

    def _fail(self, job: EditJob, step: str, message: str) -> EditJob:
        job.status = JobStatus.FAILED
        job.failed_step = step
        job.error = message
        job.finished_at = time.time()
        logger.error("Job %s failed at step %s: %s", job.job_id, step, message)
        return job
