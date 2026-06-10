"""Watch-folder orchestrator: video file in -> highlight clips out.

Per-file state machine:

    DISCOVERED -> ANALYZING -> GENERATING -> EDITING -> ARCHIVING -> DONE
                      |             |           |           |
                      +------------ FAILED <----+-----------+

Flow per new video in the watch dir:
  1. ANALYZING  — VideoIntelligence.analyze()
  2. GENERATING — SegmentGenerator.generate(); write CSV EDL (audit artifact)
                  and the '<basename>-proj.llc' sidecar (the file LosslessCut
                  auto-loads when the video is opened headlessly)
  3. EDITING    — PipelineController job: open -> [import] -> export -> close
  4. ARCHIVING  — move video + artifacts into archive/
On any failure the video + an '<basename>.error.json' report move to failed/.

New-file detection is poll-based with a size-stability check so files still
being copied into the watch dir are not picked up mid-write.
"""

import json
import logging
import shutil
import time
import traceback
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable, Optional

from backend.domain.services.pipeline.controller import JobStatus, PipelineController
from backend.domain.services.pipeline.segment_generator import (
    SegmentGenerator,
    write_edl_csv,
    write_llc_sidecar,
)
from backend.domain.services.pipeline.video_intelligence import VideoIntelligence

logger = logging.getLogger(__name__)

DEFAULT_VIDEO_EXTS = (".mp4", ".mkv", ".mov", ".ts", ".flv", ".webm", ".avi", ".m4v")


class PipelineStage(str, Enum):
    DISCOVERED = "discovered"
    ANALYZING = "analyzing"
    GENERATING = "generating"
    EDITING = "editing"
    ARCHIVING = "archiving"
    DONE = "done"
    FAILED = "failed"


@dataclass
class PipelineJob:
    """Tracks one video's trip through the pipeline."""

    video_path: Path
    stage: PipelineStage = PipelineStage.DISCOVERED
    error: Optional[str] = None
    segments_source: Optional[str] = None  # "llm" | "fallback"
    segment_count: int = 0
    edl_path: Optional[Path] = None
    sidecar_path: Optional[Path] = None
    started_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None


@dataclass
class OrchestratorConfig:
    watch_dir: Path
    archive_dir: Optional[Path] = None  # default: <watch_dir>/archive
    failed_dir: Optional[Path] = None  # default: <watch_dir>/failed
    intent: str = "Extract the most interesting highlight moments"
    poll_interval: float = 10.0
    video_exts: tuple = DEFAULT_VIDEO_EXTS

    def __post_init__(self):
        self.watch_dir = Path(self.watch_dir)
        self.archive_dir = Path(self.archive_dir) if self.archive_dir else self.watch_dir / "archive"
        self.failed_dir = Path(self.failed_dir) if self.failed_dir else self.watch_dir / "failed"


class WatchFolderOrchestrator:
    """Polls a folder and runs intelligence -> segment-gen -> LosslessCut."""

    def __init__(
        self,
        config: OrchestratorConfig,
        intelligence: Optional[VideoIntelligence] = None,
        generator: Optional[SegmentGenerator] = None,
        controller: Optional[PipelineController] = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.config = config
        self.intelligence = intelligence or VideoIntelligence()
        self.generator = generator or SegmentGenerator()
        self.controller = controller or PipelineController()
        self._sleep = sleep
        self._last_sizes: dict[Path, int] = {}
        self.history: list[PipelineJob] = []

        for d in (self.config.watch_dir, self.config.archive_dir, self.config.failed_dir):
            d.mkdir(parents=True, exist_ok=True)

    # ── Discovery ─────────────────────────────────────────────────

    def discover(self) -> list[Path]:
        """Return videos in the watch dir whose size is stable since last poll.

        First sighting records the size; the file is only returned once a
        subsequent poll sees the same size (copy finished).
        """
        ready: list[Path] = []
        seen: set[Path] = set()
        for entry in sorted(self.config.watch_dir.iterdir()):
            if not entry.is_file() or entry.suffix.lower() not in self.config.video_exts:
                continue
            seen.add(entry)
            try:
                size = entry.stat().st_size
            except OSError:
                continue
            if self._last_sizes.get(entry) == size:
                ready.append(entry)
            self._last_sizes[entry] = size
        # Forget files that disappeared (processed or removed externally).
        self._last_sizes = {p: s for p, s in self._last_sizes.items() if p in seen}
        return ready

    # ── Per-file pipeline ─────────────────────────────────────────

    def process_file(self, video_path: Path) -> PipelineJob:
        job = PipelineJob(video_path=Path(video_path))
        self.history.append(job)
        logger.info("Pipeline start: %s", video_path)

        try:
            # 1. Analyze
            job.stage = PipelineStage.ANALYZING
            analysis = self.intelligence.analyze(str(video_path))

            # 2. Generate segments + write EDL artifacts
            job.stage = PipelineStage.GENERATING
            result = self.generator.generate(analysis, self.config.intent)
            if not result.segments:
                raise RuntimeError("Segment generation produced no segments")
            job.segments_source = result.source
            job.segment_count = len(result.segments)
            job.edl_path = write_edl_csv(
                result.segments, job.video_path.with_suffix(job.video_path.suffix + ".edl.csv")
            )
            job.sidecar_path = write_llc_sidecar(result.segments, job.video_path)

            # 3. Drive LosslessCut (sidecar is auto-loaded on open)
            job.stage = PipelineStage.EDITING
            edit_job = self.controller.submit_job(str(job.video_path), edl_path=str(job.edl_path))
            edit_job = self.controller.run_job(edit_job)
            if edit_job.status != JobStatus.COMPLETED:
                raise RuntimeError(f"LosslessCut job failed at step {edit_job.failed_step}: {edit_job.error}")

            # 4. Archive source + artifacts
            job.stage = PipelineStage.ARCHIVING
            self._move_with_artifacts(job, self.config.archive_dir)

            job.stage = PipelineStage.DONE
            job.finished_at = time.time()
            logger.info(
                "Pipeline done: %s (%d segments via %s)",
                video_path,
                job.segment_count,
                job.segments_source,
            )
        except Exception as exc:
            self._handle_failure(job, exc)
        return job

    def _handle_failure(self, job: PipelineJob, exc: Exception) -> None:
        failed_stage = job.stage.value
        job.error = str(exc)
        job.stage = PipelineStage.FAILED
        job.finished_at = time.time()
        logger.exception("Pipeline failed at %s: %s", failed_stage, job.video_path)
        try:
            report = {
                "video": job.video_path.name,
                "stage": failed_stage,
                "error": str(exc),
                "traceback": traceback.format_exc(),
                "timestamp": time.time(),
            }
            self.config.failed_dir.mkdir(parents=True, exist_ok=True)
            error_path = self.config.failed_dir / f"{job.video_path.stem}.error.json"
            error_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
            self._move_with_artifacts(job, self.config.failed_dir)
        except Exception:
            # Error recovery must never crash the watch loop.
            logger.exception("Failed to move %s to failed dir", job.video_path)

    def _move_with_artifacts(self, job: PipelineJob, dest_dir: Path) -> None:
        dest_dir.mkdir(parents=True, exist_ok=True)
        for path in (job.video_path, job.edl_path, job.sidecar_path):
            if path is None:
                continue
            path = Path(path)
            if not path.exists():
                continue
            target = dest_dir / path.name
            if target.exists():
                target = dest_dir / f"{path.stem}.{int(time.time())}{path.suffix}"
            shutil.move(str(path), str(target))
        self._last_sizes.pop(job.video_path, None)

    # ── Loop ──────────────────────────────────────────────────────

    def poll_once(self) -> list[PipelineJob]:
        """One discovery pass; processes every ready file sequentially."""
        jobs = []
        for video in self.discover():
            jobs.append(self.process_file(video))
        return jobs

    def run_once(self) -> list[PipelineJob]:
        """Single-shot run: sighting pass, short settle, then process.

        The size-stability check needs two discover() passes; a fresh
        process running with --once would otherwise never see a stable file.
        """
        self.discover()
        self._sleep(min(self.config.poll_interval, 2.0))
        return self.poll_once()

    def run_forever(self, max_iterations: Optional[int] = None) -> None:
        """Poll until interrupted (or max_iterations, for tests/smoke runs)."""
        logger.info(
            "Watching %s (intent: %s, poll every %.0fs)",
            self.config.watch_dir,
            self.config.intent,
            self.config.poll_interval,
        )
        iterations = 0
        try:
            while max_iterations is None or iterations < max_iterations:
                try:
                    self.poll_once()
                except Exception:
                    logger.exception("Poll iteration crashed — continuing")
                iterations += 1
                if max_iterations is None or iterations < max_iterations:
                    self._sleep(self.config.poll_interval)
        except KeyboardInterrupt:
            logger.info("Interrupted — shutting down")
        finally:
            self.controller.shutdown()
