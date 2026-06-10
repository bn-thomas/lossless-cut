"""Tests for backend.domain.services.pipeline.video_intelligence.

All ffprobe/ffmpeg invocations are mocked with canned CLI output captured
from real runs — no video files or ffmpeg binary required.
"""

import json
import subprocess
from unittest.mock import patch

import pytest

from backend.domain.services.pipeline.video_intelligence import (
    IntelligenceConfig,
    VideoAnalysisError,
    VideoIntelligence,
)

# ── Canned CLI outputs ────────────────────────────────────────────

FFPROBE_JSON = json.dumps(
    {
        "streams": [
            {
                "index": 0,
                "codec_type": "video",
                "codec_name": "h264",
                "width": 1920,
                "height": 1080,
                "avg_frame_rate": "60/1",
            },
            {
                "index": 1,
                "codec_type": "audio",
                "codec_name": "aac",
                "sample_rate": "48000",
                "channels": 2,
            },
        ],
        "format": {
            "format_name": "mov,mp4,m4a,3gp,3g2,mj2",
            "duration": "120.500000",
            "size": "104857600",
            "bit_rate": "6960000",
        },
    }
)

# Real-world silencedetect stderr shape (interleaved with decoder noise).
SILENCEDETECT_STDERR = """\
Input #0, mov,mp4,m4a,3gp,3g2,mj2, from 'vod.mp4':
  Duration: 00:02:00.50, start: 0.000000, bitrate: 6960 kb/s
[silencedetect @ 0x55d1c2b0] silence_start: 10.2
[silencedetect @ 0x55d1c2b0] silence_end: 14.75 | silence_duration: 4.55
frame= 1000 fps=0.0 q=-0.0 size=N/A time=00:00:40.00 bitrate=N/A
[silencedetect @ 0x55d1c2b0] silence_start: 60
[silencedetect @ 0x55d1c2b0] silence_end: 63.5 | silence_duration: 3.5
size=N/A time=00:02:00.50 bitrate=N/A speed= 312x
"""

# metadata=print stderr: frame line (pts_time) followed by the score line.
SCENE_STDERR = """\
Input #0, mov,mp4,m4a,3gp,3g2,mj2, from 'vod.mp4':
[Parsed_metadata_1 @ 0x5634] frame:0    pts:301301   pts_time:12.554
[Parsed_metadata_1 @ 0x5634] lavfi.scene_score=0.474
[Parsed_metadata_1 @ 0x5634] frame:1    pts:1080080  pts_time:45.003
[Parsed_metadata_1 @ 0x5634] lavfi.scene_score=0.812
[Parsed_metadata_1 @ 0x5634] frame:2    pts:2160160  pts_time:90.007
[Parsed_metadata_1 @ 0x5634] lavfi.scene_score=0.533
"""


def proc(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


@pytest.fixture
def vi():
    return VideoIntelligence()


# ── probe_media ───────────────────────────────────────────────────


class TestProbeMedia:
    @patch("backend.domain.services.pipeline.video_intelligence.subprocess.run")
    def test_parses_streams_and_duration(self, mock_run, vi):
        mock_run.return_value = proc(stdout=FFPROBE_JSON)

        meta = vi.probe_media("/v/vod.mp4")

        assert meta["duration"] == pytest.approx(120.5)
        assert meta["format_name"].startswith("mov,mp4")
        assert meta["has_video"] is True
        assert meta["has_audio"] is True
        assert meta["streams"][0]["codec_name"] == "h264"
        assert meta["streams"][0]["width"] == 1920
        assert meta["streams"][1]["channels"] == 2

    @patch("backend.domain.services.pipeline.video_intelligence.subprocess.run")
    def test_command_shape(self, mock_run, vi):
        mock_run.return_value = proc(stdout=FFPROBE_JSON)
        vi.probe_media("/v/vod.mp4")

        cmd = mock_run.call_args.args[0]
        assert cmd[0] == "ffprobe"
        assert "-show_streams" in cmd
        assert "-show_format" in cmd
        assert cmd[-1] == "/v/vod.mp4"

    @patch("backend.domain.services.pipeline.video_intelligence.subprocess.run")
    def test_duration_fallback_to_stream(self, mock_run, vi):
        payload = json.dumps(
            {
                "streams": [{"index": 0, "codec_type": "video", "duration": "33.3"}],
                "format": {"format_name": "matroska"},
            }
        )
        mock_run.return_value = proc(stdout=payload)

        assert vi.probe_media("/v/x.mkv")["duration"] == pytest.approx(33.3)

    @patch("backend.domain.services.pipeline.video_intelligence.subprocess.run")
    def test_nonzero_exit_raises(self, mock_run, vi):
        mock_run.return_value = proc(returncode=1, stderr="No such file or directory")
        with pytest.raises(VideoAnalysisError, match="ffprobe failed"):
            vi.probe_media("/v/missing.mp4")

    @patch("backend.domain.services.pipeline.video_intelligence.subprocess.run")
    def test_bad_json_raises(self, mock_run, vi):
        mock_run.return_value = proc(stdout="not json")
        with pytest.raises(VideoAnalysisError, match="invalid JSON"):
            vi.probe_media("/v/vod.mp4")

    @patch("backend.domain.services.pipeline.video_intelligence.subprocess.run")
    def test_missing_duration_raises(self, mock_run, vi):
        mock_run.return_value = proc(stdout=json.dumps({"streams": [], "format": {}}))
        with pytest.raises(VideoAnalysisError, match="duration"):
            vi.probe_media("/v/vod.mp4")

    @patch(
        "backend.domain.services.pipeline.video_intelligence.subprocess.run",
        side_effect=FileNotFoundError,
    )
    def test_missing_executable_raises(self, _mock, vi):
        with pytest.raises(VideoAnalysisError, match="not found"):
            vi.probe_media("/v/vod.mp4")

    @patch(
        "backend.domain.services.pipeline.video_intelligence.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="ffprobe", timeout=600),
    )
    def test_timeout_raises(self, _mock, vi):
        with pytest.raises(VideoAnalysisError, match="timed out"):
            vi.probe_media("/v/vod.mp4")


# ── detect_silence ────────────────────────────────────────────────


class TestDetectSilence:
    @patch("backend.domain.services.pipeline.video_intelligence.subprocess.run")
    def test_parses_pairs_from_stderr(self, mock_run, vi):
        mock_run.return_value = proc(stderr=SILENCEDETECT_STDERR)

        silences = vi.detect_silence("/v/vod.mp4")

        assert silences == [
            {"start": 10.2, "end": 14.75},
            {"start": 60.0, "end": 63.5},
        ]

    @patch("backend.domain.services.pipeline.video_intelligence.subprocess.run")
    def test_filter_uses_config_values(self, mock_run):
        vi = VideoIntelligence(IntelligenceConfig(silence_noise_db=-40, silence_min_duration=2.0))
        mock_run.return_value = proc(stderr="")
        vi.detect_silence("/v/vod.mp4")

        cmd = mock_run.call_args.args[0]
        af = cmd[cmd.index("-af") + 1]
        assert "noise=-40" in af
        assert "d=2.0" in af

    @patch("backend.domain.services.pipeline.video_intelligence.subprocess.run")
    def test_unterminated_silence_runs_to_eof(self, mock_run, vi):
        mock_run.return_value = proc(stderr="[silencedetect @ 0x1] silence_start: 100.5\n")
        assert vi.detect_silence("/v/vod.mp4") == [{"start": 100.5, "end": None}]

    @patch("backend.domain.services.pipeline.video_intelligence.subprocess.run")
    def test_negative_start_clamped_to_zero(self, mock_run, vi):
        mock_run.return_value = proc(
            stderr=(
                "[silencedetect @ 0x1] silence_start: -0.02\n"
                "[silencedetect @ 0x1] silence_end: 3.0 | silence_duration: 3.02\n"
            )
        )
        assert vi.detect_silence("/v/vod.mp4") == [{"start": 0.0, "end": 3.0}]

    @patch("backend.domain.services.pipeline.video_intelligence.subprocess.run")
    def test_no_silence_lines(self, mock_run, vi):
        mock_run.return_value = proc(stderr="frame= 100 fps=25\n")
        assert vi.detect_silence("/v/vod.mp4") == []

    @patch("backend.domain.services.pipeline.video_intelligence.subprocess.run")
    def test_nonzero_exit_raises(self, mock_run, vi):
        mock_run.return_value = proc(returncode=1, stderr="Invalid data")
        with pytest.raises(VideoAnalysisError, match="silencedetect failed"):
            vi.detect_silence("/v/vod.mp4")


# ── detect_scene_changes ──────────────────────────────────────────


class TestDetectSceneChanges:
    @patch("backend.domain.services.pipeline.video_intelligence.subprocess.run")
    def test_parses_time_score_pairs(self, mock_run, vi):
        mock_run.return_value = proc(stderr=SCENE_STDERR)

        changes = vi.detect_scene_changes("/v/vod.mp4")

        assert changes == [
            {"time": 12.554, "score": 0.474},
            {"time": 45.003, "score": 0.812},
            {"time": 90.007, "score": 0.533},
        ]

    @patch("backend.domain.services.pipeline.video_intelligence.subprocess.run")
    def test_threshold_in_filter(self, mock_run):
        vi = VideoIntelligence(IntelligenceConfig(scene_threshold=0.25))
        mock_run.return_value = proc(stderr="")
        vi.detect_scene_changes("/v/vod.mp4")

        cmd = mock_run.call_args.args[0]
        vf = cmd[cmd.index("-vf") + 1]
        assert "gt(scene\\,0.25)" in vf
        assert "metadata=print" in vf

    @patch("backend.domain.services.pipeline.video_intelligence.subprocess.run")
    def test_no_scene_changes(self, mock_run, vi):
        mock_run.return_value = proc(stderr="Input #0 ...\n")
        assert vi.detect_scene_changes("/v/vod.mp4") == []

    @patch("backend.domain.services.pipeline.video_intelligence.subprocess.run")
    def test_nonzero_exit_raises(self, mock_run, vi):
        mock_run.return_value = proc(returncode=187, stderr="filter error")
        with pytest.raises(VideoAnalysisError, match="scene detection failed"):
            vi.detect_scene_changes("/v/vod.mp4")


# ── analyze (combined) ────────────────────────────────────────────


class TestAnalyze:
    @patch("backend.domain.services.pipeline.video_intelligence.subprocess.run")
    def test_full_analysis_structure(self, mock_run, vi):
        mock_run.side_effect = [
            proc(stdout=FFPROBE_JSON),  # probe
            proc(stderr=SILENCEDETECT_STDERR),  # silencedetect
            proc(stderr=SCENE_STDERR),  # scene
        ]

        analysis = vi.analyze("/v/vod.mp4")

        assert analysis["path"] == "/v/vod.mp4"
        assert analysis["duration"] == pytest.approx(120.5)
        assert len(analysis["silences"]) == 2
        assert len(analysis["scene_changes"]) == 3

        kinds = {s["kind"] for s in analysis["segments"]}
        assert kinds == {"scene", "active", "silence"}

        # Scene spans partition [0, duration] at the cut points.
        scenes = [s for s in analysis["segments"] if s["kind"] == "scene"]
        assert scenes[0]["start"] == 0.0
        assert scenes[0]["end"] == pytest.approx(12.554)
        assert scenes[0]["score"] == 0.5  # first span default
        assert scenes[1]["score"] == pytest.approx(0.474)  # opening cut's score
        assert scenes[-1]["end"] == pytest.approx(120.5)

        # Active spans complement the silences.
        active = [s for s in analysis["segments"] if s["kind"] == "active"]
        assert active[0] == {"start": 0.0, "end": 10.2, "kind": "active", "score": 1.0}
        assert active[-1]["end"] == pytest.approx(120.5)

        # Every segment lies within bounds and is well-formed.
        for seg in analysis["segments"]:
            assert 0.0 <= seg["start"] < seg["end"] <= 120.5

    @patch("backend.domain.services.pipeline.video_intelligence.subprocess.run")
    def test_audio_only_file_skips_scene_pass(self, mock_run, vi):
        audio_probe = json.dumps(
            {
                "streams": [{"index": 0, "codec_type": "audio", "codec_name": "mp3"}],
                "format": {"format_name": "mp3", "duration": "30.0"},
            }
        )
        mock_run.side_effect = [proc(stdout=audio_probe), proc(stderr="")]

        analysis = vi.analyze("/v/audio.mp3")

        assert analysis["scene_changes"] == []
        assert mock_run.call_count == 2  # no ffmpeg scene pass

    @patch("backend.domain.services.pipeline.video_intelligence.subprocess.run")
    def test_video_only_file_skips_silence_pass(self, mock_run, vi):
        video_probe = json.dumps(
            {
                "streams": [{"index": 0, "codec_type": "video", "codec_name": "h264"}],
                "format": {"format_name": "mp4", "duration": "30.0"},
            }
        )
        mock_run.side_effect = [proc(stdout=video_probe), proc(stderr="")]

        analysis = vi.analyze("/v/silent.mp4")

        assert analysis["silences"] == []
        # One whole-video active span still emitted.
        active = [s for s in analysis["segments"] if s["kind"] == "active"]
        assert active == [{"start": 0.0, "end": 30.0, "kind": "active", "score": 1.0}]

    @patch("backend.domain.services.pipeline.video_intelligence.subprocess.run")
    def test_eof_silence_clamped_to_duration(self, mock_run, vi):
        mock_run.side_effect = [
            proc(stdout=FFPROBE_JSON),
            proc(stderr="[silencedetect @ 0x1] silence_start: 110\n"),
            proc(stderr=""),
        ]

        analysis = vi.analyze("/v/vod.mp4")

        assert analysis["silences"] == [{"start": 110.0, "end": 120.5}]

    @patch("backend.domain.services.pipeline.video_intelligence.subprocess.run")
    def test_scene_changes_beyond_duration_dropped(self, mock_run, vi):
        bogus_scene = (
            "[Parsed_metadata_1 @ 0x1] frame:0 pts:1 pts_time:500.0\n[Parsed_metadata_1 @ 0x1] lavfi.scene_score=0.9\n"
        )
        mock_run.side_effect = [
            proc(stdout=FFPROBE_JSON),
            proc(stderr=""),
            proc(stderr=bogus_scene),
        ]

        analysis = vi.analyze("/v/vod.mp4")

        assert analysis["scene_changes"] == []
