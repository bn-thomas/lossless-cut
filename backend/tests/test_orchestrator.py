"""Tests for backend.domain.services.pipeline.orchestrator.

Everything external is mocked: intelligence, generator, and controller.
Files are real (tmp_path) so the move-to-archive/failed behaviour is tested
for real, but no ffmpeg/LLM/Electron is touched.
"""

import json
from unittest.mock import MagicMock

import pytest

from backend.domain.services.pipeline.controller import JobStatus
from backend.domain.services.pipeline.orchestrator import (
    OrchestratorConfig,
    PipelineStage,
    WatchFolderOrchestrator,
)
from backend.domain.services.pipeline.segment_generator import GenerationResult, Segment

# ── Fixtures ──────────────────────────────────────────────────────


ANALYSIS = {
    "path": "x",
    "duration": 100.0,
    "metadata": {},
    "silences": [],
    "scene_changes": [],
    "segments": [{"start": 0.0, "end": 100.0, "kind": "scene", "score": 0.5}],
}


@pytest.fixture
def intelligence():
    mock = MagicMock()
    mock.analyze.return_value = ANALYSIS
    return mock


@pytest.fixture
def generator():
    mock = MagicMock()
    mock.generate.return_value = GenerationResult(
        segments=[Segment(10.0, 30.0, "Clip A"), Segment(50.0, 70.0, "Clip B")],
        source="llm",
        reasoning="test",
    )
    return mock


@pytest.fixture
def controller():
    mock = MagicMock()

    def run_job(job):
        job.status = JobStatus.COMPLETED
        return job

    def submit_job(video_path, edl_path=None):
        from backend.domain.services.pipeline.controller import EditJob

        return EditJob(video_path=video_path, edl_path=edl_path)

    mock.submit_job.side_effect = submit_job
    mock.run_job.side_effect = run_job
    return mock


@pytest.fixture
def orchestrator(tmp_path, intelligence, generator, controller):
    config = OrchestratorConfig(watch_dir=tmp_path / "inbox", poll_interval=0)
    return WatchFolderOrchestrator(
        config=config,
        intelligence=intelligence,
        generator=generator,
        controller=controller,
        sleep=lambda _t: None,
    )


def drop_video(orchestrator, name="vod.mp4", content=b"fake video bytes"):
    path = orchestrator.config.watch_dir / name
    path.write_bytes(content)
    return path


# ── Config ────────────────────────────────────────────────────────


class TestConfig:
    def test_default_subdirs(self, tmp_path):
        config = OrchestratorConfig(watch_dir=tmp_path / "in")
        assert config.archive_dir == tmp_path / "in" / "archive"
        assert config.failed_dir == tmp_path / "in" / "failed"

    def test_dirs_created_on_init(self, orchestrator):
        assert orchestrator.config.watch_dir.is_dir()
        assert orchestrator.config.archive_dir.is_dir()
        assert orchestrator.config.failed_dir.is_dir()


# ── Discovery ─────────────────────────────────────────────────────


class TestDiscovery:
    def test_new_file_needs_stable_size(self, orchestrator):
        video = drop_video(orchestrator)

        assert orchestrator.discover() == []  # first sighting: record size
        assert orchestrator.discover() == [video]  # stable: ready

    def test_growing_file_not_ready(self, orchestrator):
        video = drop_video(orchestrator, content=b"part")
        orchestrator.discover()
        video.write_bytes(b"part + more data")  # still copying

        assert orchestrator.discover() == []
        assert orchestrator.discover() == [video]  # stabilized now

    def test_non_video_files_ignored(self, orchestrator):
        (orchestrator.config.watch_dir / "notes.txt").write_text("hi")
        (orchestrator.config.watch_dir / "thumb.png").write_bytes(b"x")
        orchestrator.discover()

        assert orchestrator.discover() == []

    def test_subdirectories_ignored(self, orchestrator):
        # archive/ and failed/ live under the watch dir by default.
        orchestrator.discover()
        assert orchestrator.discover() == []

    def test_extension_case_insensitive(self, orchestrator):
        video = drop_video(orchestrator, name="CLIP.MP4")
        orchestrator.discover()
        assert orchestrator.discover() == [video]


# ── Happy path ────────────────────────────────────────────────────


class TestProcessFile:
    def test_full_chain(self, orchestrator, intelligence, generator, controller):
        video = drop_video(orchestrator)

        job = orchestrator.process_file(video)

        assert job.stage == PipelineStage.DONE
        assert job.error is None
        assert job.segments_source == "llm"
        assert job.segment_count == 2

        intelligence.analyze.assert_called_once_with(str(video))
        generator.generate.assert_called_once()
        assert generator.generate.call_args.args[1] == orchestrator.config.intent
        controller.run_job.assert_called_once()

        # Source + artifacts archived; watch dir empty.
        archive = orchestrator.config.archive_dir
        assert (archive / "vod.mp4").exists()
        assert (archive / "vod.mp4.edl.csv").exists()
        assert (archive / "vod-proj.llc").exists()
        assert not video.exists()

    def test_artifacts_written_before_edit(self, orchestrator, controller):
        """The LLC sidecar must exist when the controller job runs (auto-load)."""
        video = drop_video(orchestrator)
        sidecar = video.parent / "vod-proj.llc"

        seen = {}

        def run_job(job):
            seen["sidecar_exists"] = sidecar.exists()
            job.status = JobStatus.COMPLETED
            return job

        controller.run_job.side_effect = run_job
        orchestrator.process_file(video)

        assert seen["sidecar_exists"] is True

    def test_sidecar_content_matches_segments(self, orchestrator):
        video = drop_video(orchestrator)
        orchestrator.process_file(video)

        sidecar = orchestrator.config.archive_dir / "vod-proj.llc"
        project = json.loads(sidecar.read_text(encoding="utf-8"))
        assert project["mediaFileName"] == "vod.mp4"
        assert [s["name"] for s in project["cutSegments"]] == ["Clip A", "Clip B"]

    def test_poll_once_processes_ready_files(self, orchestrator):
        drop_video(orchestrator, "a.mp4")
        drop_video(orchestrator, "b.mkv")

        assert orchestrator.poll_once() == []  # sighting pass
        jobs = orchestrator.poll_once()

        assert len(jobs) == 2
        assert all(j.stage == PipelineStage.DONE for j in jobs)
        assert orchestrator.history == jobs


# ── Failure handling ──────────────────────────────────────────────


class TestFailures:
    def _assert_failed(self, orchestrator, job, stage):
        failed_dir = orchestrator.config.failed_dir
        assert job.stage == PipelineStage.FAILED
        assert (failed_dir / "vod.mp4").exists()
        report = json.loads((failed_dir / "vod.error.json").read_text(encoding="utf-8"))
        assert report["stage"] == stage
        assert report["error"]
        assert "traceback" in report
        return report

    def test_analysis_failure(self, orchestrator, intelligence):
        intelligence.analyze.side_effect = RuntimeError("ffprobe exploded")
        video = drop_video(orchestrator)

        job = orchestrator.process_file(video)

        report = self._assert_failed(orchestrator, job, "analyzing")
        assert "ffprobe exploded" in report["error"]
        assert not video.exists()

    def test_empty_generation_fails(self, orchestrator, generator):
        generator.generate.return_value = GenerationResult(segments=[], source="fallback")
        video = drop_video(orchestrator)

        job = orchestrator.process_file(video)

        self._assert_failed(orchestrator, job, "generating")

    def test_edit_failure_moves_artifacts_too(self, orchestrator, controller):
        def run_job(job):
            job.status = JobStatus.FAILED
            job.failed_step = "export"
            job.error = "disk full"
            return job

        controller.run_job.side_effect = run_job
        video = drop_video(orchestrator)

        job = orchestrator.process_file(video)

        report = self._assert_failed(orchestrator, job, "editing")
        assert "disk full" in report["error"]
        # EDL artifacts travel with the failed video for debugging.
        assert (orchestrator.config.failed_dir / "vod.mp4.edl.csv").exists()
        assert (orchestrator.config.failed_dir / "vod-proj.llc").exists()

    def test_failure_does_not_crash_loop(self, orchestrator, intelligence):
        intelligence.analyze.side_effect = RuntimeError("boom")
        drop_video(orchestrator, "bad.mp4")
        orchestrator.poll_once()

        jobs = orchestrator.poll_once()  # must not raise

        assert jobs[0].stage == PipelineStage.FAILED

    def test_failed_file_not_rediscovered(self, orchestrator, intelligence):
        intelligence.analyze.side_effect = RuntimeError("boom")
        drop_video(orchestrator)
        orchestrator.poll_once()
        orchestrator.poll_once()

        assert orchestrator.discover() == []


# ── Loop control ──────────────────────────────────────────────────


class TestLoop:
    def test_run_once_processes_in_single_call(self, orchestrator):
        """run_once must not need a prior sighting pass (fresh-process --once)."""
        drop_video(orchestrator)

        jobs = orchestrator.run_once()

        assert len(jobs) == 1
        assert jobs[0].stage == PipelineStage.DONE

    def test_run_forever_bounded_iterations(self, orchestrator, controller):
        drop_video(orchestrator)

        orchestrator.run_forever(max_iterations=2)  # sighting + process

        assert len(orchestrator.history) == 1
        controller.shutdown.assert_called_once()

    def test_poll_crash_does_not_kill_loop(self, orchestrator, controller, monkeypatch):
        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            raise RuntimeError("transient")

        monkeypatch.setattr(orchestrator, "poll_once", flaky)
        orchestrator.run_forever(max_iterations=3)

        assert calls["n"] == 3
        controller.shutdown.assert_called_once()
