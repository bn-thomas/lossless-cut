"""AI segment generator: analysis + user intent -> highlight segments + EDL.

LLM call: raw httpx against the Anthropic Messages API (the ``anthropic`` SDK
is not a dependency of this fork's backend). Config via env:
  ANTHROPIC_API_KEY    — required for LLM mode (falls back to heuristics if unset)
  PIPELINE_LLM_MODEL   — default "claude-fable-5"

Output formats (verified against upstream src/renderer/src):
  CSV EDL  — edlFormats.ts parseCsv/formatCsvSeconds: optional header row
             "Start,End,Name", then one row per segment with start/end as
             plain decimal seconds and a free-text name.
  LLC      — types.ts llcProjectV2Schema: {"version": 2, "mediaFileName",
             "cutSegments": [{"start", "end", "name"}]}. Written as plain
             JSON (valid JSON5, which loadLlcProject parses). Saved next to
             the video as "<basename>-proj.llc" it is auto-loaded on open.

Failure behaviour: any API error, malformed JSON, or out-of-bounds response
falls back to the top-N scene segments by score (never raises mid-pipeline).
"""

import csv
import io
import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-fable-5"
ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"

_SEGMENTS_SCHEMA = {
    "type": "object",
    "properties": {
        "segments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "start": {"type": "number"},
                    "end": {"type": "number"},
                    "name": {"type": "string"},
                },
                "required": ["start", "end", "name"],
                "additionalProperties": False,
            },
        },
        "reasoning": {"type": "string"},
    },
    "required": ["segments", "reasoning"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """\
You select highlight segments from a video for lossless cutting.

You receive an automated analysis (scene spans with change scores, audio \
activity/silence spans, duration) and a user intent. Choose the segments that \
best satisfy the intent.

Rules:
- Segment times must lie within [0, duration] and have start < end.
- Prefer cutting at scene boundaries; avoid starting or ending inside silence \
unless the intent asks for it.
- Merge adjacent candidate spans when they form one coherent moment.
- Give each segment a short descriptive name.
- Respect the requested maximum number of segments."""


@dataclass
class Segment:
    start: float
    end: float
    name: str = ""

    @property
    def duration(self) -> float:
        return self.end - self.start


@dataclass
class GenerationResult:
    segments: list[Segment]
    source: str  # "llm" | "fallback"
    reasoning: str = ""
    warnings: list = field(default_factory=list)


class SegmentGenerator:
    """Turn an intelligence analysis + intent into validated highlight segments."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        max_segments: int = 8,
        min_segment_seconds: float = 2.0,
        max_tokens: int = 4096,
        timeout: float = 120.0,
        client: Optional[httpx.Client] = None,
    ):
        self.api_key = api_key if api_key is not None else os.environ.get("ANTHROPIC_API_KEY", "")
        self.model = model or os.environ.get("PIPELINE_LLM_MODEL", DEFAULT_MODEL)
        self.max_segments = max_segments
        self.min_segment_seconds = min_segment_seconds
        self.max_tokens = max_tokens
        self.timeout = timeout
        self._client = client

    # ── Public entry point ────────────────────────────────────────

    def generate(self, analysis: dict, intent: str) -> GenerationResult:
        """Select highlight segments. Never raises; falls back on any failure."""
        if not self.api_key:
            logger.warning("ANTHROPIC_API_KEY not set — using heuristic fallback")
            return self._fallback(analysis, ["no API key configured"])

        try:
            raw_text = self._call_llm(analysis, intent)
        except Exception as exc:
            logger.exception("LLM call failed")
            return self._fallback(analysis, [f"LLM call failed: {exc}"])

        parsed = self._parse_response(raw_text)
        if parsed is None:
            logger.error("LLM returned unparseable output: %.200s", raw_text)
            return self._fallback(analysis, ["LLM output was not valid JSON"])

        segments, warnings = self._validate(parsed.get("segments", []), analysis["duration"])
        if not segments:
            warnings.append("no valid segments after validation")
            return self._fallback(analysis, warnings)

        return GenerationResult(
            segments=segments,
            source="llm",
            reasoning=str(parsed.get("reasoning", "")),
            warnings=warnings,
        )

    # ── LLM plumbing ──────────────────────────────────────────────

    def _call_llm(self, analysis: dict, intent: str) -> str:
        payload = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": self._build_prompt(analysis, intent)}],
            "output_config": {"format": {"type": "json_schema", "schema": _SEGMENTS_SCHEMA}},
        }
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
        }
        client = self._client or httpx.Client(timeout=self.timeout)
        try:
            resp = client.post(ANTHROPIC_API_URL, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
        finally:
            if self._client is None:
                client.close()

        for block in data.get("content", []):
            if block.get("type") == "text":
                return block["text"]
        raise ValueError("No text block in API response")

    def _build_prompt(self, analysis: dict, intent: str) -> str:
        duration = analysis["duration"]
        lines = [
            f"User intent: {intent}",
            f"Video duration: {duration:.2f} seconds",
            f"Select at most {self.max_segments} segments, each at least {self.min_segment_seconds:.1f} seconds long.",
            "",
            "Candidate segments (start, end, kind, score):",
        ]
        # Cap the table so huge VODs do not blow the prompt up.
        for seg in analysis.get("segments", [])[:400]:
            lines.append(f"- {seg['start']:.2f} -> {seg['end']:.2f}  {seg['kind']}  score={seg['score']:.2f}")
        lines += [
            "",
            'Respond with JSON: {"segments": [{"start": <sec>, "end": <sec>, '
            '"name": "<short label>"}], "reasoning": "<one paragraph>"}',
        ]
        return "\n".join(lines)

    # ── Parsing & validation ──────────────────────────────────────

    @staticmethod
    def _parse_response(text: str) -> Optional[dict]:
        """Parse the LLM's JSON, tolerating wrapping prose/code fences."""
        for candidate in (text, _extract_json_object(text)):
            if not candidate:
                continue
            try:
                parsed = json.loads(candidate)
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(parsed, dict) and isinstance(parsed.get("segments"), list):
                return parsed
        return None

    def _validate(self, raw_segments: list, duration: float) -> tuple[list[Segment], list[str]]:
        """Clamp to [0, duration], drop invalid/short rows, sort, de-overlap."""
        warnings: list[str] = []
        cleaned: list[Segment] = []
        for i, raw in enumerate(raw_segments):
            if not isinstance(raw, dict):
                warnings.append(f"segment {i}: not an object — dropped")
                continue
            try:
                start = float(raw["start"])
                end = float(raw["end"])
            except (KeyError, TypeError, ValueError):
                warnings.append(f"segment {i}: missing/non-numeric start or end — dropped")
                continue
            clamped_start = max(0.0, min(start, duration))
            clamped_end = max(0.0, min(end, duration))
            if (clamped_start, clamped_end) != (start, end):
                warnings.append(f"segment {i}: clamped to video bounds")
            if clamped_end - clamped_start < self.min_segment_seconds:
                warnings.append(f"segment {i}: shorter than minimum — dropped")
                continue
            name = str(raw.get("name", "") or f"Segment {len(cleaned) + 1}")
            cleaned.append(Segment(start=clamped_start, end=clamped_end, name=name))

        cleaned.sort(key=lambda s: (s.start, s.end))

        # Remove overlaps: keep earlier segment, trim or drop the later one.
        non_overlapping: list[Segment] = []
        for seg in cleaned:
            if non_overlapping and seg.start < non_overlapping[-1].end:
                new_start = non_overlapping[-1].end
                if seg.end - new_start >= self.min_segment_seconds:
                    warnings.append(f"segment '{seg.name}': trimmed overlap")
                    seg = Segment(start=new_start, end=seg.end, name=seg.name)
                else:
                    warnings.append(f"segment '{seg.name}': overlapped — dropped")
                    continue
            non_overlapping.append(seg)

        if len(non_overlapping) > self.max_segments:
            warnings.append(f"truncated to {self.max_segments} segments")
            non_overlapping = non_overlapping[: self.max_segments]
        return non_overlapping, warnings

    # ── Heuristic fallback ────────────────────────────────────────

    def _fallback(self, analysis: dict, warnings: list) -> GenerationResult:
        """Top-N scene segments by score (longest-first tiebreak)."""
        duration = analysis["duration"]
        scenes = [
            s
            for s in analysis.get("segments", [])
            if s["kind"] == "scene" and (s["end"] - s["start"]) >= self.min_segment_seconds
        ]
        scenes.sort(key=lambda s: (s["score"], s["end"] - s["start"]), reverse=True)
        top = scenes[: self.max_segments]
        top.sort(key=lambda s: s["start"])
        segments = [
            Segment(
                start=max(0.0, s["start"]),
                end=min(duration, s["end"]),
                name=f"Highlight {i + 1}",
            )
            for i, s in enumerate(top)
        ]
        if not segments and duration > 0:
            # Degenerate input (no scenes detected): keep the whole video.
            segments = [Segment(start=0.0, end=duration, name="Full video")]
        return GenerationResult(
            segments=segments,
            source="fallback",
            reasoning="heuristic top-N scene selection",
            warnings=list(warnings),
        )


# ── EDL emission (formats verified against upstream renderer code) ──


def format_edl_csv(segments: list[Segment]) -> str:
    """CSV EDL in LosslessCut's import format: Start,End,Name + seconds rows."""
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(["Start", "End", "Name"])
    for seg in segments:
        writer.writerow([_fmt_seconds(seg.start), _fmt_seconds(seg.end), seg.name])
    return buf.getvalue()


def write_edl_csv(segments: list[Segment], path) -> Path:
    path = Path(path)
    path.write_text(format_edl_csv(segments), encoding="utf-8")
    return path


def format_llc_project(segments: list[Segment], media_file_name: str) -> str:
    """LosslessCut project JSON matching llcProjectV2Schema (name is required)."""
    project = {
        "version": 2,
        "mediaFileName": media_file_name,
        "cutSegments": [{"start": seg.start, "end": seg.end, "name": seg.name} for seg in segments],
    }
    return json.dumps(project, indent=2)


def write_llc_sidecar(segments: list[Segment], video_path) -> Path:
    """Write '<basename>-proj.llc' next to the video (auto-loaded on open).

    Upstream resolves the sidecar as getSuffixedOutPath(nameSuffix='proj.llc')
    => '<stem>-proj.llc' in the video's directory.
    """
    video_path = Path(video_path)
    sidecar = video_path.with_name(f"{video_path.stem}-proj.llc")
    sidecar.write_text(format_llc_project(segments, video_path.name), encoding="utf-8")
    return sidecar


def _fmt_seconds(value: float) -> str:
    # Plain decimal seconds, no scientific notation; trim trailing zeros the
    # same way String(Number) does for round values.
    text = f"{value:.6f}".rstrip("0").rstrip(".")
    return text or "0"


def _extract_json_object(text: str) -> Optional[str]:
    """Pull the first balanced {...} block out of prose/code-fenced output."""
    if not text:
        return None
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_string = False
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None
