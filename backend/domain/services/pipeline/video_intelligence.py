"""Pure-Python video analysis via ffprobe/ffmpeg subprocesses.

Three probes, all parse-only (no video decoding in Python):
  - probe_media:          ffprobe -print_format json (streams/format/duration)
  - detect_silence:       ffmpeg silencedetect audio filter (parse stderr)
  - detect_scene_changes: ffmpeg select=gt(scene,T),metadata=print (parse stderr)

``analyze()`` combines them into a structured analysis dict:

    {
      "path": str,
      "duration": float,
      "metadata": {...},                 # condensed ffprobe info
      "silences": [{"start", "end"}],
      "scene_changes": [{"time", "score"}],
      "segments": [
        {"start": float, "end": float, "kind": "scene"|"active"|"silence",
         "score": float}
      ],
    }

Segment kinds:
  scene   — span between consecutive scene-change cuts (score = scene score
            of the cut that opens the span; first span scores 0.5)
  active  — non-silent audio span (complement of detected silences)
  silence — detected silent span (score 0.0)
"""

import json
import logging
import re
import subprocess
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

# ffmpeg silencedetect writes to stderr, e.g.:
#   [silencedetect @ 0x...] silence_start: 12.345
#   [silencedetect @ 0x...] silence_end: 15.6 | silence_duration: 3.255
_SILENCE_START_RE = re.compile(r"silence_start:\s*(-?[\d.]+)")
_SILENCE_END_RE = re.compile(r"silence_end:\s*(-?[\d.]+)")

# ffmpeg metadata=print writes to stderr, e.g.:
#   [Parsed_metadata_1 @ 0x...] frame:3 pts:301301 pts_time:12.554
#   [Parsed_metadata_1 @ 0x...] lavfi.scene_score=0.474
_PTS_TIME_RE = re.compile(r"pts_time:([\d.]+)")
_SCENE_SCORE_RE = re.compile(r"lavfi\.scene_score=([\d.]+)")


class VideoAnalysisError(Exception):
    """Raised when ffprobe/ffmpeg fails or returns unusable output."""


@dataclass
class IntelligenceConfig:
    ffprobe_path: str = "ffprobe"
    ffmpeg_path: str = "ffmpeg"
    silence_noise_db: float = -30.0
    silence_min_duration: float = 0.75
    scene_threshold: float = 0.4
    subprocess_timeout: float = 600.0


class VideoIntelligence:
    """ffprobe/ffmpeg-backed analysis producing highlight candidates."""

    def __init__(self, config: Optional[IntelligenceConfig] = None):
        self.config = config or IntelligenceConfig()

    # ── Subprocess plumbing ───────────────────────────────────────

    def _run(self, cmd: list[str]) -> subprocess.CompletedProcess:
        logger.debug("Running: %s", " ".join(cmd))
        try:
            return subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.config.subprocess_timeout,
            )
        except FileNotFoundError as exc:
            raise VideoAnalysisError(f"Executable not found: {cmd[0]}") from exc
        except subprocess.TimeoutExpired as exc:
            raise VideoAnalysisError(f"Command timed out: {' '.join(cmd)}") from exc

    # ── ffprobe metadata ──────────────────────────────────────────

    def probe_media(self, path: str) -> dict:
        """Return condensed stream/format metadata for a media file."""
        cmd = [
            self.config.ffprobe_path,
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            path,
        ]
        proc = self._run(cmd)
        if proc.returncode != 0:
            raise VideoAnalysisError(f"ffprobe failed ({proc.returncode}): {proc.stderr.strip()}")
        try:
            raw = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            raise VideoAnalysisError("ffprobe returned invalid JSON") from exc

        fmt = raw.get("format", {})
        duration = _to_float(fmt.get("duration"))
        streams = []
        for s in raw.get("streams", []):
            streams.append(
                {
                    "index": s.get("index"),
                    "codec_type": s.get("codec_type"),
                    "codec_name": s.get("codec_name"),
                    "width": s.get("width"),
                    "height": s.get("height"),
                    "avg_frame_rate": s.get("avg_frame_rate"),
                    "sample_rate": s.get("sample_rate"),
                    "channels": s.get("channels"),
                }
            )
            # Some containers only carry duration on the streams.
            if duration is None:
                duration = _to_float(s.get("duration"))

        if duration is None:
            raise VideoAnalysisError(f"Could not determine duration for {path}")

        return {
            "path": path,
            "duration": duration,
            "format_name": fmt.get("format_name"),
            "bit_rate": _to_float(fmt.get("bit_rate")),
            "size_bytes": _to_float(fmt.get("size")),
            "streams": streams,
            "has_video": any(s["codec_type"] == "video" for s in streams),
            "has_audio": any(s["codec_type"] == "audio" for s in streams),
        }

    # ── Silence detection ─────────────────────────────────────────

    def detect_silence(self, path: str) -> list[dict]:
        """Return [{"start", "end"}] silent spans via the silencedetect filter."""
        af = f"silencedetect=noise={self.config.silence_noise_db}dB:d={self.config.silence_min_duration}"
        cmd = [
            self.config.ffmpeg_path,
            "-hide_banner",
            "-nostats",
            "-i",
            path,
            "-af",
            af,
            "-vn",
            "-f",
            "null",
            "-",
        ]
        proc = self._run(cmd)
        if proc.returncode != 0:
            raise VideoAnalysisError(f"ffmpeg silencedetect failed ({proc.returncode}): {proc.stderr.strip()[-500:]}")
        return self._parse_silences(proc.stderr)

    @staticmethod
    def _parse_silences(stderr: str) -> list[dict]:
        silences: list[dict] = []
        pending_start: Optional[float] = None
        for line in stderr.splitlines():
            m = _SILENCE_START_RE.search(line)
            if m:
                pending_start = max(0.0, float(m.group(1)))
                continue
            m = _SILENCE_END_RE.search(line)
            if m and pending_start is not None:
                end = float(m.group(1))
                if end > pending_start:
                    silences.append({"start": pending_start, "end": end})
                pending_start = None
        # A silence_start with no matching end runs to EOF; callers clamp
        # against duration when building segments.
        if pending_start is not None:
            silences.append({"start": pending_start, "end": None})
        return silences

    # ── Scene-change detection ────────────────────────────────────

    def detect_scene_changes(self, path: str) -> list[dict]:
        """Return [{"time", "score"}] scene-change candidates.

        Uses ``select=gt(scene,T),metadata=print`` so each selected frame's
        pts_time and lavfi.scene_score land on stderr as adjacent lines.
        """
        vf = f"select=gt(scene\\,{self.config.scene_threshold}),metadata=print"
        cmd = [
            self.config.ffmpeg_path,
            "-hide_banner",
            "-nostats",
            "-i",
            path,
            "-vf",
            vf,
            "-an",
            "-f",
            "null",
            "-",
        ]
        proc = self._run(cmd)
        if proc.returncode != 0:
            raise VideoAnalysisError(f"ffmpeg scene detection failed ({proc.returncode}): {proc.stderr.strip()[-500:]}")
        return self._parse_scene_changes(proc.stderr)

    @staticmethod
    def _parse_scene_changes(stderr: str) -> list[dict]:
        changes: list[dict] = []
        pending_time: Optional[float] = None
        for line in stderr.splitlines():
            m = _PTS_TIME_RE.search(line)
            if m:
                pending_time = float(m.group(1))
                continue
            m = _SCENE_SCORE_RE.search(line)
            if m and pending_time is not None:
                changes.append({"time": pending_time, "score": float(m.group(1))})
                pending_time = None
        return changes

    # ── Combined analysis ─────────────────────────────────────────

    def analyze(self, path: str) -> dict:
        """Run all probes and emit the structured analysis dict."""
        metadata = self.probe_media(path)
        duration = metadata["duration"]

        silences: list[dict] = []
        if metadata["has_audio"]:
            silences = self._clamp_silences(self.detect_silence(path), duration)

        scene_changes: list[dict] = []
        if metadata["has_video"]:
            scene_changes = [c for c in self.detect_scene_changes(path) if c["time"] < duration]

        segments = self._build_segments(duration, silences, scene_changes)
        return {
            "path": path,
            "duration": duration,
            "metadata": metadata,
            "silences": silences,
            "scene_changes": scene_changes,
            "segments": segments,
        }

    @staticmethod
    def _clamp_silences(silences: list[dict], duration: float) -> list[dict]:
        clamped = []
        for s in silences:
            start = min(s["start"], duration)
            end = duration if s["end"] is None else min(s["end"], duration)
            if end > start:
                clamped.append({"start": start, "end": end})
        return clamped

    @staticmethod
    def _build_segments(duration: float, silences: list[dict], scene_changes: list[dict]) -> list[dict]:
        segments: list[dict] = []

        # Scene spans: cut points partition [0, duration].
        cut_times = [c["time"] for c in scene_changes]
        boundaries = [0.0, *cut_times, duration]
        scores = [0.5, *(c["score"] for c in scene_changes)]
        for i in range(len(boundaries) - 1):
            start, end = boundaries[i], boundaries[i + 1]
            if end - start > 0.01:
                segments.append({"start": start, "end": end, "kind": "scene", "score": scores[i]})

        # Silence + active (audio-activity complement) spans.
        cursor = 0.0
        for s in silences:
            if s["start"] - cursor > 0.01:
                segments.append({"start": cursor, "end": s["start"], "kind": "active", "score": 1.0})
            segments.append({"start": s["start"], "end": s["end"], "kind": "silence", "score": 0.0})
            cursor = s["end"]
        if duration - cursor > 0.01:
            segments.append({"start": cursor, "end": duration, "kind": "active", "score": 1.0})

        segments.sort(key=lambda seg: (seg["start"], seg["end"]))
        return segments


def _to_float(value) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
