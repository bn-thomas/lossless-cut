"""Smoke tests for the pipeline CLI (backend.domain.services.pipeline.cli).

Verifies arg/env wiring and the --once execution path with all heavy
components mocked. No LosslessCut, ffmpeg, or LLM involved.
"""

from unittest.mock import MagicMock, patch

from backend.domain.services.pipeline import cli


class TestParser:
    def test_args_override_env(self, monkeypatch, tmp_path):
        monkeypatch.setenv("PIPELINE_WATCH_DIR", "/env/watch")
        monkeypatch.setenv("PIPELINE_INTENT", "env intent")
        args = cli.build_parser().parse_args(["--watch-dir", str(tmp_path), "--intent", "cli intent"])
        assert args.watch_dir == str(tmp_path)
        assert args.intent == "cli intent"

    def test_env_fallback(self, monkeypatch):
        monkeypatch.setenv("PIPELINE_WATCH_DIR", "/env/watch")
        monkeypatch.setenv("PIPELINE_POLL_INTERVAL", "2.5")
        monkeypatch.setenv("LOSSLESSCUT_PORT", "9999")
        args = cli.build_parser().parse_args([])
        assert args.watch_dir == "/env/watch"
        assert args.poll_interval == 2.5
        assert args.port == 9999

    def test_defaults(self, monkeypatch):
        for var in (
            "PIPELINE_WATCH_DIR",
            "PIPELINE_INTENT",
            "PIPELINE_POLL_INTERVAL",
            "PIPELINE_LLM_MODEL",
            "LOSSLESSCUT_PORT",
            "LOSSLESSCUT_PATH",
        ):
            monkeypatch.delenv(var, raising=False)
        args = cli.build_parser().parse_args([])
        assert args.watch_dir is None
        assert args.poll_interval == 10.0
        assert args.port == 8080
        assert args.once is False


class TestBuildOrchestrator:
    def test_wiring(self, monkeypatch, tmp_path):
        monkeypatch.delenv("PIPELINE_LLM_MODEL", raising=False)
        args = cli.build_parser().parse_args(
            [
                "--watch-dir",
                str(tmp_path / "in"),
                "--model",
                "claude-fable-5",
                "--max-segments",
                "5",
                "--port",
                "8123",
                "--losslesscut-path",
                "/opt/LosslessCut",
                "--intent",
                "goals only",
            ]
        )

        orch = cli.build_orchestrator(args)

        assert orch.config.watch_dir == tmp_path / "in"
        assert orch.config.intent == "goals only"
        assert orch.generator.model == "claude-fable-5"
        assert orch.generator.max_segments == 5
        assert orch.controller.service.http_port == 8123
        assert orch.controller.service.executable_path == "/opt/LosslessCut"


class TestMain:
    def test_missing_watch_dir_errors(self, monkeypatch, capsys):
        monkeypatch.delenv("PIPELINE_WATCH_DIR", raising=False)
        assert cli.main([]) == 2
        assert "--watch-dir" in capsys.readouterr().err

    @patch("backend.domain.services.pipeline.cli.build_orchestrator")
    def test_once_success(self, mock_build, tmp_path, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        orch = MagicMock()
        ok_job = MagicMock()
        ok_job.error = None
        orch.run_once.return_value = [ok_job]
        mock_build.return_value = orch

        rc = cli.main(["--watch-dir", str(tmp_path), "--once"])

        assert rc == 0
        orch.run_once.assert_called_once()
        orch.controller.shutdown.assert_called_once()

    @patch("backend.domain.services.pipeline.cli.build_orchestrator")
    def test_once_with_failures_returns_nonzero(self, mock_build, tmp_path, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        orch = MagicMock()
        bad_job = MagicMock()
        bad_job.error = "boom"
        orch.run_once.return_value = [bad_job]
        mock_build.return_value = orch

        assert cli.main(["--watch-dir", str(tmp_path), "--once"]) == 1

    @patch("backend.domain.services.pipeline.cli.build_orchestrator")
    def test_loop_mode_calls_run_forever(self, mock_build, tmp_path, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        orch = MagicMock()
        mock_build.return_value = orch

        assert cli.main(["--watch-dir", str(tmp_path)]) == 0
        orch.run_forever.assert_called_once()

    def test_end_to_end_smoke_with_mocked_components(self, tmp_path, monkeypatch):
        """--once against a real orchestrator with mocked heavy dependencies."""
        from backend.domain.services.pipeline import orchestrator as orch_mod
        from backend.domain.services.pipeline.controller import JobStatus
        from backend.domain.services.pipeline.segment_generator import (
            GenerationResult,
            Segment,
        )

        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        watch = tmp_path / "inbox"
        watch.mkdir()
        video = watch / "vod.mp4"
        video.write_bytes(b"data")

        analysis = {
            "path": str(video),
            "duration": 50.0,
            "metadata": {},
            "silences": [],
            "scene_changes": [],
            "segments": [{"start": 0.0, "end": 50.0, "kind": "scene", "score": 0.6}],
        }

        def fake_intel(self, path):
            return analysis

        def fake_generate(self, a, intent):
            return GenerationResult(segments=[Segment(5.0, 25.0, "Clip")], source="fallback")

        def fake_run_job(self, job):
            job.status = JobStatus.COMPLETED
            return job

        monkeypatch.setattr(orch_mod.VideoIntelligence, "analyze", fake_intel)
        monkeypatch.setattr("backend.domain.services.pipeline.cli.SegmentGenerator.generate", fake_generate)
        monkeypatch.setattr("backend.domain.services.pipeline.cli.PipelineController.run_job", fake_run_job)
        monkeypatch.setattr("backend.domain.services.pipeline.cli.LosslessCutService.stop", lambda self: True)

        # run_once does the sighting pass + settle + processing pass itself.
        assert cli.main(["--watch-dir", str(watch), "--poll-interval", "0", "--once"]) == 0
        assert (watch / "archive" / "vod.mp4").exists()
        assert (watch / "archive" / "vod-proj.llc").exists()
