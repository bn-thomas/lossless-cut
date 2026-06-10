"""Tests for LosslessCut subprocess control service.

Covers:
  - Subprocess lifecycle (start, stop, is_running)
  - Health-check polling during startup
  - HTTP action dispatch (import_edl, export, close_file, seek, open_files, send_action)
  - Error handling (process not running, HTTP failures, connection errors)
"""

import subprocess
from unittest.mock import MagicMock, patch

import pytest
import requests

from backend.domain.services.losslesscut_service import LosslessCutService

# ── Fixtures ──────────────────────────────────────────────────────


@pytest.fixture
def service():
    """Service instance with default settings."""
    return LosslessCutService()


@pytest.fixture
def service_custom():
    """Service with custom port and executable."""
    return LosslessCutService(
        http_port=9090,
        http_host="0.0.0.0",
        executable_path="/opt/LosslessCut",
    )


@pytest.fixture
def running_service(service):
    """Service with a mocked running subprocess."""
    mock_proc = MagicMock(spec=subprocess.Popen)
    mock_proc.poll.return_value = None  # process is alive
    mock_proc.pid = 12345
    service._process = mock_proc
    return service


# ── Constructor & properties ──────────────────────────────────────


class TestInit:
    def test_defaults(self, service):
        assert service.http_port == 8080
        assert service.http_host == "127.0.0.1"
        assert service.executable_path is None
        assert service._process is None

    def test_custom_config(self, service_custom):
        assert service_custom.http_port == 9090
        assert service_custom.http_host == "0.0.0.0"
        assert service_custom.executable_path == "/opt/LosslessCut"

    def test_base_url(self, service):
        assert service.base_url == "http://127.0.0.1:8080"

    def test_base_url_custom(self, service_custom):
        assert service_custom.base_url == "http://0.0.0.0:9090"

    def test_action_url(self, service):
        assert service._action_url("export") == "http://127.0.0.1:8080/api/action/export"
        assert service._action_url("goToTimecodeDirect") == "http://127.0.0.1:8080/api/action/goToTimecodeDirect"


# ── Executable resolution ────────────────────────────────────────


class TestResolveExecutable:
    def test_explicit_path(self, service_custom):
        assert service_custom._resolve_executable() == "/opt/LosslessCut"

    @patch("backend.domain.services.losslesscut_service.Path.is_file", return_value=False)
    def test_fallback_to_path(self, _mock, service):
        assert service._resolve_executable() == "LosslessCut"

    @patch("backend.domain.services.losslesscut_service.Path.is_file")
    def test_finds_candidate(self, mock_is_file, service):
        # Only /usr/local/bin/LosslessCut exists
        mock_is_file.side_effect = lambda: True
        # Since all candidates use the same Path.is_file, the first match wins
        result = service._resolve_executable()
        assert "LosslessCut" in result or "losslesscut" in result


# ── is_running ────────────────────────────────────────────────────


class TestIsRunning:
    def test_no_process(self, service):
        assert service.is_running() is False

    def test_running_process(self, running_service):
        assert running_service.is_running() is True

    def test_exited_process(self, service):
        mock_proc = MagicMock(spec=subprocess.Popen)
        mock_proc.poll.return_value = 0  # exited
        service._process = mock_proc
        assert service.is_running() is False


# ── start ─────────────────────────────────────────────────────────


class TestStart:
    @patch.object(LosslessCutService, "_wait_for_ready", return_value=True)
    @patch("subprocess.Popen")
    def test_start_success(self, mock_popen, mock_wait, service):
        mock_proc = MagicMock()
        mock_proc.pid = 1000
        mock_proc.poll.return_value = None
        mock_popen.return_value = mock_proc

        result = service.start()

        assert result is True
        assert service._process is mock_proc
        mock_popen.assert_called_once_with(
            ["LosslessCut", "--http-api", "8080"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    @patch.object(LosslessCutService, "_wait_for_ready", return_value=True)
    @patch("subprocess.Popen")
    def test_start_with_files(self, mock_popen, mock_wait, service):
        mock_proc = MagicMock()
        mock_proc.pid = 1000
        mock_proc.poll.return_value = None
        mock_popen.return_value = mock_proc

        service.start(files=["/tmp/video.mp4", "/tmp/video2.mkv"])

        mock_popen.assert_called_once_with(
            ["LosslessCut", "--http-api", "8080", "/tmp/video.mp4", "/tmp/video2.mkv"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    @patch.object(LosslessCutService, "_wait_for_ready", return_value=True)
    @patch("subprocess.Popen")
    def test_start_custom_port(self, mock_popen, mock_wait, service_custom):
        mock_proc = MagicMock()
        mock_proc.pid = 1000
        mock_proc.poll.return_value = None
        mock_popen.return_value = mock_proc

        service_custom.start()

        mock_popen.assert_called_once_with(
            ["/opt/LosslessCut", "--http-api", "9090"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    @patch.object(LosslessCutService, "_wait_for_ready", return_value=False)
    @patch.object(LosslessCutService, "stop", return_value=True)
    @patch("subprocess.Popen")
    def test_start_health_check_fails(self, mock_popen, mock_stop, mock_wait, service):
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_popen.return_value = mock_proc

        result = service.start()

        assert result is False
        mock_stop.assert_called_once()

    @patch("subprocess.Popen", side_effect=FileNotFoundError("not found"))
    def test_start_executable_not_found(self, mock_popen, service):
        result = service.start()
        assert result is False
        assert service._process is None

    def test_start_already_running(self, running_service):
        result = running_service.start()
        assert result is True  # Returns True without re-launching


# ── _wait_for_ready ───────────────────────────────────────────────


class TestWaitForReady:
    @patch("time.sleep")
    @patch("requests.get")
    def test_ready_first_attempt(self, mock_get, mock_sleep, running_service):
        mock_get.return_value = MagicMock(status_code=200)

        result = running_service._wait_for_ready(max_retries=3, delay=0.01)

        assert result is True
        mock_get.assert_called_once_with("http://127.0.0.1:8080", timeout=2)
        mock_sleep.assert_not_called()

    @patch("time.sleep")
    @patch("requests.get")
    def test_ready_after_retries(self, mock_get, mock_sleep, running_service):
        mock_get.side_effect = [
            requests.ConnectionError("refused"),
            requests.ConnectionError("refused"),
            MagicMock(status_code=200),
        ]

        result = running_service._wait_for_ready(max_retries=5, delay=0.01)

        assert result is True
        assert mock_get.call_count == 3
        assert mock_sleep.call_count == 2

    @patch("time.sleep")
    @patch("requests.get", side_effect=requests.ConnectionError("refused"))
    def test_never_ready(self, mock_get, mock_sleep, running_service):
        result = running_service._wait_for_ready(max_retries=3, delay=0.01)

        assert result is False
        assert mock_get.call_count == 3

    @patch("time.sleep")
    @patch("requests.get")
    def test_process_dies_during_wait(self, mock_get, mock_sleep, service):
        mock_proc = MagicMock(spec=subprocess.Popen)
        mock_proc.poll.return_value = 1  # process exited with error
        mock_proc.returncode = 1
        service._process = mock_proc

        mock_get.side_effect = requests.ConnectionError("refused")

        result = service._wait_for_ready(max_retries=5, delay=0.01)
        assert result is False


# ── stop ──────────────────────────────────────────────────────────


class TestStop:
    def test_stop_no_process(self, service):
        assert service.stop() is True

    @patch("requests.post")
    def test_stop_graceful_quit(self, mock_post, running_service):
        mock_post.return_value = MagicMock(status_code=200)
        running_service._process.wait.return_value = 0

        result = running_service.stop()

        assert result is True
        assert running_service._process is None
        mock_post.assert_called_once_with(
            "http://127.0.0.1:8080/api/action/quit",
            timeout=5,
        )

    @patch("requests.post", side_effect=requests.ConnectionError("refused"))
    def test_stop_quit_fails_terminate_succeeds(self, mock_post, running_service):
        proc = running_service._process
        # quit POST raises ConnectionError → except Exception catches → falls to terminate
        # terminate succeeds, wait returns normally
        proc.wait.return_value = 0

        result = running_service.stop()

        assert result is True
        assert running_service._process is None
        proc.terminate.assert_called_once()

    @patch("requests.post", side_effect=requests.ConnectionError("refused"))
    def test_stop_terminate_timeout_then_kill(self, mock_post, running_service):
        proc = running_service._process
        # quit fails (ConnectionError) → fall to terminate
        # terminate's wait times out → fall to kill
        # kill's wait succeeds
        proc.wait.side_effect = [
            subprocess.TimeoutExpired(cmd="LosslessCut", timeout=10),  # after terminate()
            0,  # after kill()
        ]

        result = running_service.stop()

        assert result is True
        assert running_service._process is None
        proc.terminate.assert_called_once()
        proc.kill.assert_called_once()


# ── HTTP action dispatch ──────────────────────────────────────────


class TestPostAction:
    def test_not_running(self, service):
        result = service._post_action("export")
        assert result == {"status": "error", "message": "LosslessCut is not running"}

    @patch("requests.post")
    def test_success_no_body(self, mock_post, running_service):
        mock_post.return_value = MagicMock(status_code=200)
        mock_post.return_value.raise_for_status = MagicMock()

        result = running_service._post_action("export")

        assert result == {"status": "ok"}
        mock_post.assert_called_once_with(
            "http://127.0.0.1:8080/api/action/export",
            timeout=10,
        )

    @patch("requests.post")
    def test_success_with_body(self, mock_post, running_service):
        mock_post.return_value = MagicMock(status_code=200)
        mock_post.return_value.raise_for_status = MagicMock()

        result = running_service._post_action("goToTimecodeDirect", {"time": "09:11"})

        assert result == {"status": "ok"}
        mock_post.assert_called_once_with(
            "http://127.0.0.1:8080/api/action/goToTimecodeDirect",
            timeout=10,
            json={"time": "09:11"},
        )

    @patch("requests.post")
    def test_http_error(self, mock_post, running_service):
        resp = MagicMock()
        resp.status_code = 500
        resp.raise_for_status.side_effect = requests.HTTPError(response=resp)
        mock_post.return_value = resp

        result = running_service._post_action("export")

        assert result["status"] == "error"
        assert "500" in result["message"]

    @patch("requests.post", side_effect=requests.ConnectionError("refused"))
    def test_connection_error(self, mock_post, running_service):
        result = running_service._post_action("export")

        assert result["status"] == "error"
        assert "refused" in result["message"]


# ── High-level action methods ─────────────────────────────────────


class TestActionMethods:
    @patch.object(LosslessCutService, "_post_action", return_value={"status": "ok"})
    def test_import_edl(self, mock_action, running_service):
        result = running_service.import_edl("/tmp/cuts.edl")

        mock_action.assert_called_once_with("importEdlFile", {"path": "/tmp/cuts.edl"}, timeout=30)
        assert result == {"status": "ok"}

    @patch.object(LosslessCutService, "_post_action", return_value={"status": "ok"})
    def test_export(self, mock_action, running_service):
        result = running_service.export()

        mock_action.assert_called_once_with("export", timeout=300)
        assert result == {"status": "ok"}

    @patch.object(LosslessCutService, "_post_action", return_value={"status": "ok"})
    def test_close_file(self, mock_action, running_service):
        result = running_service.close_file()

        mock_action.assert_called_once_with("closeCurrentFile")
        assert result == {"status": "ok"}

    @patch.object(LosslessCutService, "_post_action", return_value={"status": "ok"})
    def test_seek(self, mock_action, running_service):
        result = running_service.seek("00:05:30")

        mock_action.assert_called_once_with("goToTimecodeDirect", {"time": "00:05:30"})
        assert result == {"status": "ok"}

    @patch.object(LosslessCutService, "_post_action", return_value={"status": "ok"})
    def test_open_files(self, mock_action, running_service):
        result = running_service.open_files(["/tmp/a.mp4", "/tmp/b.mkv"])

        # Upstream openFilesActionArgsSchema expects a bare JSON array body.
        mock_action.assert_called_once_with("openFiles", ["/tmp/a.mp4", "/tmp/b.mkv"], timeout=30)
        assert result == {"status": "ok"}

    @patch.object(LosslessCutService, "_post_action", return_value={"status": "ok"})
    def test_send_action_no_params(self, mock_action, running_service):
        result = running_service.send_action("togglePlayResetSpeed")

        mock_action.assert_called_once_with("togglePlayResetSpeed", None, timeout=10)
        assert result == {"status": "ok"}

    @patch.object(LosslessCutService, "_post_action", return_value={"status": "ok"})
    def test_send_action_with_params(self, mock_action, running_service):
        result = running_service.send_action("goToTimecodeDirect", {"time": "01:00"}, timeout=5)

        mock_action.assert_called_once_with("goToTimecodeDirect", {"time": "01:00"}, timeout=5)
        assert result == {"status": "ok"}

    def test_methods_fail_when_not_running(self, service):
        """All action methods return error when process is not running."""
        for method, args in [
            (service.import_edl, ("/tmp/cuts.edl",)),
            (service.export, ()),
            (service.close_file, ()),
            (service.seek, ("00:00:00",)),
            (service.open_files, (["/tmp/a.mp4"],)),
            (service.send_action, ("play",)),
        ]:
            result = method(*args)
            assert result["status"] == "error"
            assert "not running" in result["message"]
