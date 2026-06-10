"""Integration seam test: PipelineController -> LosslessCutService -> HTTP API.

Uses a REAL LosslessCutService (no method mocks) with the ``requests``
transport mocked, and asserts the exact HTTP call sequence LosslessCut's
Express server (src/main/httpServer.ts) would receive:

    POST /api/action/openFiles      body: ["<video>"]
    POST /api/action/importEdlFile  body: {"path": "<edl>"}   (import_action)
    POST /api/action/export         no body
    POST /api/action/closeCurrentFile

This pins the wire format (URLs + body shapes) against the upstream API spec
in docs/api.md without launching Electron.
"""

from unittest.mock import MagicMock, patch

import pytest

from backend.domain.services.losslesscut_service import LosslessCutService
from backend.domain.services.pipeline.controller import JobStatus, PipelineController

BASE = "http://127.0.0.1:8080"


@pytest.fixture
def running_service():
    """Real service wired to a fake live subprocess."""
    svc = LosslessCutService()
    proc = MagicMock()
    proc.poll.return_value = None  # alive
    proc.pid = 4242
    svc._process = proc
    return svc


def _http_ok():
    resp = MagicMock()
    resp.status_code = 200
    resp.raise_for_status.return_value = None
    return resp


class TestHttpSeam:
    @patch("backend.domain.services.losslesscut_service.requests.post")
    def test_open_import_export_sequence(self, mock_post, running_service):
        """The canonical open-file -> import-EDL -> export call sequence."""
        mock_post.return_value = _http_ok()
        controller = PipelineController(
            service=running_service,
            edl_strategy="import_action",
            open_settle_seconds=0,
        )

        job = controller.submit_job("/videos/vod.mp4", edl_path="/videos/vod.edl.csv")
        controller.run_next()

        assert job.status == JobStatus.COMPLETED

        calls = mock_post.call_args_list
        urls = [c.args[0] for c in calls]
        assert urls == [
            f"{BASE}/api/action/openFiles",
            f"{BASE}/api/action/importEdlFile",
            f"{BASE}/api/action/export",
            f"{BASE}/api/action/closeCurrentFile",
        ]

        # Body shapes match what the upstream zod schemas / action handlers expect.
        assert calls[0].kwargs["json"] == ["/videos/vod.mp4"]  # bare array
        assert calls[1].kwargs["json"] == {"path": "/videos/vod.edl.csv"}
        assert "json" not in calls[2].kwargs  # export takes no body
        assert "json" not in calls[3].kwargs

    @patch("backend.domain.services.losslesscut_service.requests.post")
    def test_sidecar_strategy_never_calls_import(self, mock_post, running_service):
        """Default headless flow relies on the -proj.llc sidecar, not importEdlFile."""
        mock_post.return_value = _http_ok()
        controller = PipelineController(service=running_service, open_settle_seconds=0)

        job = controller.submit_job("/videos/vod.mp4", edl_path="/videos/vod.edl.csv")
        controller.run_next()

        assert job.status == JobStatus.COMPLETED
        urls = [c.args[0] for c in mock_post.call_args_list]
        assert f"{BASE}/api/action/importEdlFile" not in urls
        assert urls[0] == f"{BASE}/api/action/openFiles"
        assert f"{BASE}/api/action/export" in urls

    @patch("backend.domain.services.losslesscut_service.requests.post")
    def test_http_error_during_export_fails_job(self, mock_post, running_service):
        """An HTTP 500 from the export action surfaces as a failed job."""
        import requests as requests_lib

        ok = _http_ok()
        error_resp = MagicMock()
        error_resp.status_code = 500
        http_error = requests_lib.HTTPError(response=error_resp)
        failing = MagicMock()
        failing.raise_for_status.side_effect = http_error

        def route(url, **kwargs):
            return failing if url.endswith("/api/action/export") else ok

        mock_post.side_effect = route
        controller = PipelineController(service=running_service, open_settle_seconds=0)

        job = controller.submit_job("/videos/vod.mp4")
        controller.run_next()

        assert job.status == JobStatus.FAILED
        assert job.failed_step == "export"
        assert "500" in job.error
