"""Tests for backend.domain.services.pipeline.controller.

Covers:
  - Job submission / queue ordering / lookup
  - run_job step sequencing for both EDL strategies
  - Step-level failure handling (open / import / export / close)
  - Lifecycle delegation (ensure_running, shutdown)
"""

from unittest.mock import MagicMock

import pytest

from backend.domain.services.pipeline.controller import (
    JobStatus,
    PipelineController,
)

# ── Fixtures ──────────────────────────────────────────────────────


@pytest.fixture
def service():
    """A LosslessCutService double that is already running and succeeds."""
    svc = MagicMock()
    svc.is_running.return_value = True
    svc.open_files.return_value = {"status": "ok"}
    svc.import_edl.return_value = {"status": "ok"}
    svc.export.return_value = {"status": "ok"}
    svc.close_file.return_value = {"status": "ok"}
    svc.start.return_value = True
    svc.stop.return_value = True
    return svc


@pytest.fixture
def controller(service):
    return PipelineController(service=service, open_settle_seconds=0)


@pytest.fixture
def import_controller(service):
    return PipelineController(service=service, edl_strategy="import_action", open_settle_seconds=0)


# ── Construction ──────────────────────────────────────────────────


class TestInit:
    def test_invalid_strategy_rejected(self, service):
        with pytest.raises(ValueError, match="edl_strategy"):
            PipelineController(service=service, edl_strategy="bogus")

    def test_default_strategy_is_sidecar(self, service):
        assert PipelineController(service=service).edl_strategy == "llc_sidecar"


# ── Queue management ──────────────────────────────────────────────


class TestQueue:
    def test_submit_returns_pending_job(self, controller):
        job = controller.submit_job("/v/a.mp4", edl_path="/v/a.edl.csv")

        assert job.status == JobStatus.PENDING
        assert job.video_path == "/v/a.mp4"
        assert job.edl_path == "/v/a.edl.csv"
        assert controller.pending_count == 1
        assert controller.get_job(job.job_id) is job

    def test_get_unknown_job(self, controller):
        assert controller.get_job("nope") is None

    def test_run_next_empty_queue(self, controller):
        assert controller.run_next() is None

    def test_fifo_order(self, controller):
        a = controller.submit_job("/v/a.mp4")
        b = controller.submit_job("/v/b.mp4")

        assert controller.run_next() is a
        assert controller.run_next() is b
        assert controller.pending_count == 0

    def test_run_all_drains_queue(self, controller):
        controller.submit_job("/v/a.mp4")
        controller.submit_job("/v/b.mp4")

        done = controller.run_all()

        assert [j.status for j in done] == [JobStatus.COMPLETED, JobStatus.COMPLETED]
        assert controller.pending_count == 0

    def test_jobs_lists_all_records(self, controller):
        controller.submit_job("/v/a.mp4")
        controller.submit_job("/v/b.mp4")
        assert len(controller.jobs) == 2


# ── Step sequencing ───────────────────────────────────────────────


class TestRunJob:
    def test_sidecar_strategy_skips_import_action(self, controller, service):
        job = controller.submit_job("/v/a.mp4", edl_path="/v/a.edl.csv")
        controller.run_next()

        service.open_files.assert_called_once_with(["/v/a.mp4"])
        service.import_edl.assert_not_called()
        service.export.assert_called_once()
        service.close_file.assert_called_once()
        assert job.status == JobStatus.COMPLETED
        assert job.steps_completed == ["open", "export", "close"]

    def test_import_strategy_dispatches_import(self, import_controller, service):
        job = import_controller.submit_job("/v/a.mp4", edl_path="/v/a.edl.csv")
        import_controller.run_next()

        service.import_edl.assert_called_once_with("/v/a.edl.csv")
        assert job.steps_completed == ["open", "import_edl", "export", "close"]
        assert job.status == JobStatus.COMPLETED

    def test_import_strategy_without_edl_skips_import(self, import_controller, service):
        job = import_controller.submit_job("/v/a.mp4")
        import_controller.run_next()

        service.import_edl.assert_not_called()
        assert job.status == JobStatus.COMPLETED

    def test_settle_sleep_between_open_and_export(self, service):
        sleep = MagicMock()
        controller = PipelineController(service=service, open_settle_seconds=3.5, sleep=sleep)
        controller.submit_job("/v/a.mp4")
        controller.run_next()

        sleep.assert_called_once_with(3.5)

    def test_timestamps_recorded(self, controller):
        job = controller.submit_job("/v/a.mp4")
        controller.run_next()

        assert job.started_at is not None
        assert job.finished_at is not None
        assert job.is_terminal


class TestFailures:
    def test_start_failure(self, service):
        service.is_running.return_value = False
        service.start.return_value = False
        controller = PipelineController(service=service, open_settle_seconds=0)
        job = controller.submit_job("/v/a.mp4")
        controller.run_next()

        assert job.status == JobStatus.FAILED
        assert job.failed_step == "start"
        service.open_files.assert_not_called()

    def test_open_failure_stops_pipeline(self, controller, service):
        service.open_files.return_value = {"status": "error", "message": "boom"}
        job = controller.submit_job("/v/a.mp4")
        controller.run_next()

        assert job.status == JobStatus.FAILED
        assert job.failed_step == "open"
        assert job.error == "boom"
        service.export.assert_not_called()

    def test_import_failure_stops_pipeline(self, import_controller, service):
        service.import_edl.return_value = {"status": "error", "message": "bad edl"}
        job = import_controller.submit_job("/v/a.mp4", edl_path="/v/a.edl.csv")
        import_controller.run_next()

        assert job.failed_step == "import_edl"
        service.export.assert_not_called()

    def test_export_failure(self, controller, service):
        service.export.return_value = {"status": "error", "message": "disk full"}
        job = controller.submit_job("/v/a.mp4")
        controller.run_next()

        assert job.status == JobStatus.FAILED
        assert job.failed_step == "export"
        assert "disk full" in job.error
        service.close_file.assert_not_called()

    def test_close_failure_is_nonfatal(self, controller, service):
        service.close_file.return_value = {"status": "error", "message": "stuck"}
        job = controller.submit_job("/v/a.mp4")
        controller.run_next()

        assert job.status == JobStatus.COMPLETED
        assert "close" not in job.steps_completed

    def test_unexpected_exception_marks_failed(self, controller, service):
        service.export.side_effect = RuntimeError("kaboom")
        job = controller.submit_job("/v/a.mp4")
        controller.run_next()

        assert job.status == JobStatus.FAILED
        assert job.failed_step == "unexpected"
        assert "kaboom" in job.error


# ── Lifecycle ─────────────────────────────────────────────────────


class TestLifecycle:
    def test_ensure_running_starts_when_down(self, service):
        service.is_running.return_value = False
        service.start.return_value = True
        controller = PipelineController(service=service)

        assert controller.ensure_running() is True
        service.start.assert_called_once()

    def test_ensure_running_noop_when_up(self, controller, service):
        assert controller.ensure_running() is True
        service.start.assert_not_called()

    def test_shutdown_stops_service(self, controller, service):
        controller.shutdown()
        service.stop.assert_called_once()

    def test_close_disabled(self, service):
        controller = PipelineController(service=service, open_settle_seconds=0, close_after_export=False)
        job = controller.submit_job("/v/a.mp4")
        controller.run_next()

        service.close_file.assert_not_called()
        assert job.status == JobStatus.COMPLETED
