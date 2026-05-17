"""LosslessCut subprocess control service.

Launches LosslessCut with --http-api flag and dispatches editing
commands via POST /api/action/:action.

See: docs/api.md and src/main/httpServer.ts for the HTTP API spec.
"""

import logging
import subprocess
import time
from pathlib import Path
from typing import Optional

import requests

logger = logging.getLogger(__name__)


class LosslessCutService:
    """Service to launch and control LosslessCut as a subprocess.

    LosslessCut exposes a single HTTP endpoint:
        POST /api/action/:action  (with optional JSON body)
    and a root GET / that returns a homepage link (used as health check).
    """

    DEFAULT_PORT = 8080
    DEFAULT_HOST = "127.0.0.1"

    def __init__(
        self,
        http_port: int = DEFAULT_PORT,
        http_host: str = DEFAULT_HOST,
        executable_path: Optional[str] = None,
    ):
        self.http_port = http_port
        self.http_host = http_host
        self.executable_path = executable_path
        self._process: Optional[subprocess.Popen] = None

    @property
    def base_url(self) -> str:
        return f"http://{self.http_host}:{self.http_port}"

    def _action_url(self, action: str) -> str:
        return f"{self.base_url}/api/action/{action}"

    def _resolve_executable(self) -> str:
        """Resolve path to LosslessCut executable."""
        if self.executable_path:
            return self.executable_path

        candidates = [
            Path("/usr/bin/LosslessCut"),
            Path("/usr/local/bin/LosslessCut"),
            Path("/usr/bin/losslesscut"),
            Path("/usr/local/bin/losslesscut"),
        ]
        for candidate in candidates:
            if candidate.is_file():
                return str(candidate)

        # Fall back to PATH lookup
        return "LosslessCut"

    # ── Subprocess lifecycle ──────────────────────────────────────

    def start(self, files: Optional[list[str]] = None) -> bool:
        """Start LosslessCut subprocess with HTTP API enabled.

        Args:
            files: Optional list of file paths to open on launch.

        Returns:
            True if the process started and the HTTP API became ready.
        """
        if self.is_running():
            logger.warning("LosslessCut is already running (pid %d)", self._process.pid)
            return True

        executable = self._resolve_executable()
        cmd = [executable, "--http-api", str(self.http_port)]
        if files:
            cmd.extend(files)

        logger.info("Launching LosslessCut: %s", " ".join(cmd))

        try:
            self._process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except FileNotFoundError:
            logger.error("LosslessCut executable not found: %s", executable)
            return False
        except Exception:
            logger.exception("Failed to launch LosslessCut")
            return False

        if not self._wait_for_ready():
            logger.error("LosslessCut HTTP API did not become ready")
            self.stop()
            return False

        logger.info("LosslessCut ready on %s:%d (pid %d)", self.http_host, self.http_port, self._process.pid)
        return True

    def stop(self) -> bool:
        """Stop LosslessCut subprocess gracefully.

        Sequence: quit action → SIGTERM → SIGKILL.
        """
        if self._process is None:
            return True

        pid = self._process.pid
        logger.info("Stopping LosslessCut (pid %d)", pid)

        # 1. Try graceful quit via HTTP API
        try:
            requests.post(self._action_url("quit"), timeout=5)
            self._process.wait(timeout=5)
            logger.info("LosslessCut quit gracefully")
            self._process = None
            return True
        except Exception:
            logger.debug("Quit action did not stop process, escalating")

        # 2. SIGTERM
        try:
            self._process.terminate()
            self._process.wait(timeout=10)
            logger.info("LosslessCut terminated via SIGTERM")
            self._process = None
            return True
        except subprocess.TimeoutExpired:
            pass

        # 3. SIGKILL
        logger.warning("Force-killing LosslessCut (pid %d)", pid)
        self._process.kill()
        self._process.wait(timeout=5)
        self._process = None
        return True

    def is_running(self) -> bool:
        """Check if the subprocess is alive."""
        return self._process is not None and self._process.poll() is None

    def _wait_for_ready(self, max_retries: int = 30, delay: float = 0.5) -> bool:
        """Poll GET / until the HTTP server responds.

        The root endpoint returns a homepage link string on success.
        """
        for attempt in range(1, max_retries + 1):
            # If the process died, stop waiting
            if self._process is not None and self._process.poll() is not None:
                logger.error("LosslessCut process exited during startup (code %d)", self._process.returncode)
                return False

            try:
                resp = requests.get(self.base_url, timeout=2)
                if resp.status_code == 200:
                    logger.debug("Health check passed on attempt %d/%d", attempt, max_retries)
                    return True
            except requests.ConnectionError:
                pass
            except Exception as exc:
                logger.debug("Health check attempt %d: %s", attempt, exc)

            time.sleep(delay)

        return False

    # ── HTTP action dispatch ──────────────────────────────────────

    def _post_action(self, action: str, body: Optional[dict] = None, timeout: float = 10) -> dict:
        """Send POST /api/action/:action.

        Returns:
            {"status": "ok"} on success, {"status": "error", "message": ...} on failure.
        """
        if not self.is_running():
            return {"status": "error", "message": "LosslessCut is not running"}

        try:
            kwargs: dict = {"timeout": timeout}
            if body is not None:
                kwargs["json"] = body
            resp = requests.post(self._action_url(action), **kwargs)
            resp.raise_for_status()
            return {"status": "ok"}
        except requests.HTTPError as exc:
            logger.error("Action %s failed: HTTP %s", action, exc.response.status_code)
            return {"status": "error", "message": f"HTTP {exc.response.status_code}"}
        except Exception as exc:
            logger.exception("Action %s failed", action)
            return {"status": "error", "message": str(exc)}

    def import_edl(self, edl_path: str) -> dict:
        """Import an EDL file (POST /api/action/importEdlFile)."""
        return self._post_action("importEdlFile", {"path": edl_path}, timeout=30)

    def export(self) -> dict:
        """Export current segments (POST /api/action/export).

        This may take a long time depending on the file size.
        """
        return self._post_action("export", timeout=300)

    def close_file(self) -> dict:
        """Close the current file (POST /api/action/closeCurrentFile)."""
        return self._post_action("closeCurrentFile")

    def seek(self, timecode: str) -> dict:
        """Seek to a timecode (POST /api/action/goToTimecodeDirect).

        Args:
            timecode: Time string, e.g. "00:05:30" or "09:11".
        """
        return self._post_action("goToTimecodeDirect", {"time": timecode})

    def open_files(self, paths: list[str]) -> dict:
        """Open files in the running instance (POST /api/action/openFiles)."""
        return self._post_action("openFiles", {"paths": paths}, timeout=30)

    def send_action(self, action: str, params: Optional[dict] = None, timeout: float = 10) -> dict:
        """Send an arbitrary keyboard action.

        This is the generic escape hatch for any of the 133 available
        actions not wrapped by a dedicated method.
        """
        return self._post_action(action, params, timeout=timeout)
