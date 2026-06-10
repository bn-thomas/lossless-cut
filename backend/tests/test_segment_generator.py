"""Tests for backend.domain.services.pipeline.segment_generator.

The Anthropic API is mocked via httpx.MockTransport — no real calls.
Covers LLM happy path, malformed output fallback, bounds validation,
heuristic fallback, and the EDL CSV/LLC emission formats.
"""

import json

import httpx

from backend.domain.services.pipeline.segment_generator import (
    ANTHROPIC_API_URL,
    DEFAULT_MODEL,
    Segment,
    SegmentGenerator,
    format_edl_csv,
    format_llc_project,
    write_edl_csv,
    write_llc_sidecar,
)

# ── Helpers ───────────────────────────────────────────────────────


ANALYSIS = {
    "path": "/v/vod.mp4",
    "duration": 120.0,
    "metadata": {},
    "silences": [{"start": 10.0, "end": 14.0}],
    "scene_changes": [
        {"time": 12.5, "score": 0.47},
        {"time": 45.0, "score": 0.81},
        {"time": 90.0, "score": 0.53},
    ],
    "segments": [
        {"start": 0.0, "end": 12.5, "kind": "scene", "score": 0.5},
        {"start": 12.5, "end": 45.0, "kind": "scene", "score": 0.47},
        {"start": 45.0, "end": 90.0, "kind": "scene", "score": 0.81},
        {"start": 90.0, "end": 120.0, "kind": "scene", "score": 0.53},
        {"start": 0.0, "end": 10.0, "kind": "active", "score": 1.0},
        {"start": 10.0, "end": 14.0, "kind": "silence", "score": 0.0},
        {"start": 14.0, "end": 120.0, "kind": "active", "score": 1.0},
    ],
}


def api_message(text: str) -> dict:
    """Minimal Messages API response envelope."""
    return {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": DEFAULT_MODEL,
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 100, "output_tokens": 50},
    }


def make_generator(handler, **kwargs) -> SegmentGenerator:
    client = httpx.Client(transport=httpx.MockTransport(handler))
    kwargs.setdefault("api_key", "sk-test")
    return SegmentGenerator(client=client, **kwargs)


def llm_returning(text: str, captured: list = None):
    def handler(request: httpx.Request) -> httpx.Response:
        if captured is not None:
            captured.append(request)
        return httpx.Response(200, json=api_message(text))

    return handler


GOOD_RESPONSE = json.dumps(
    {
        "segments": [
            {"start": 45.0, "end": 90.0, "name": "Big play"},
            {"start": 14.0, "end": 30.0, "name": "Opening rush"},
        ],
        "reasoning": "Picked the two highest-energy scenes.",
    }
)


# ── LLM happy path ────────────────────────────────────────────────


class TestGenerateLlm:
    def test_valid_response_parsed_and_sorted(self):
        gen = make_generator(llm_returning(GOOD_RESPONSE))

        result = gen.generate(ANALYSIS, "best moments")

        assert result.source == "llm"
        assert [s.name for s in result.segments] == ["Opening rush", "Big play"]
        assert result.segments[0].start == 14.0
        assert "highest-energy" in result.reasoning

    def test_request_wire_format(self):
        captured: list = []
        gen = make_generator(llm_returning(GOOD_RESPONSE, captured), model="claude-fable-5")
        gen.generate(ANALYSIS, "pull the fight scenes")

        req = captured[0]
        assert str(req.url) == ANTHROPIC_API_URL
        assert req.headers["x-api-key"] == "sk-test"
        assert req.headers["anthropic-version"] == "2023-06-01"

        payload = json.loads(req.content)
        assert payload["model"] == "claude-fable-5"
        assert payload["output_config"]["format"]["type"] == "json_schema"
        # No removed-on-Fable-5 parameters.
        assert "temperature" not in payload
        assert "thinking" not in payload
        user_msg = payload["messages"][0]["content"]
        assert "pull the fight scenes" in user_msg
        assert "120.00 seconds" in user_msg

    def test_json_wrapped_in_code_fence_still_parses(self):
        fenced = f"```json\n{GOOD_RESPONSE}\n```"
        gen = make_generator(llm_returning(fenced))

        result = gen.generate(ANALYSIS, "x")

        assert result.source == "llm"
        assert len(result.segments) == 2

    def test_json_with_prose_preamble_still_parses(self):
        gen = make_generator(llm_returning(f"Here are the segments:\n{GOOD_RESPONSE}\nEnjoy!"))
        assert gen.generate(ANALYSIS, "x").source == "llm"

    def test_default_model_from_env(self, monkeypatch):
        monkeypatch.delenv("PIPELINE_LLM_MODEL", raising=False)
        assert SegmentGenerator(api_key="k").model == DEFAULT_MODEL
        monkeypatch.setenv("PIPELINE_LLM_MODEL", "claude-haiku-4-5")
        assert SegmentGenerator(api_key="k").model == "claude-haiku-4-5"

    def test_api_key_from_env(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-from-env")
        assert SegmentGenerator().api_key == "sk-from-env"


# ── Validation against analysis bounds ────────────────────────────


class TestValidation:
    def _generate(self, segments_payload, **gen_kwargs):
        text = json.dumps({"segments": segments_payload, "reasoning": "r"})
        gen = make_generator(llm_returning(text), **gen_kwargs)
        return gen.generate(ANALYSIS, "x")

    def test_out_of_bounds_clamped(self):
        result = self._generate([{"start": -5, "end": 130, "name": "Wild"}])

        assert result.source == "llm"
        seg = result.segments[0]
        assert seg.start == 0.0
        assert seg.end == 120.0
        assert any("clamped" in w for w in result.warnings)

    def test_inverted_segment_dropped(self):
        result = self._generate(
            [
                {"start": 50, "end": 40, "name": "Backwards"},
                {"start": 10, "end": 20, "name": "Fine"},
            ]
        )
        assert [s.name for s in result.segments] == ["Fine"]

    def test_too_short_segment_dropped(self):
        result = self._generate(
            [
                {"start": 10, "end": 10.5, "name": "Blip"},
                {"start": 20, "end": 30, "name": "Fine"},
            ]
        )
        assert [s.name for s in result.segments] == ["Fine"]

    def test_non_numeric_segment_dropped(self):
        result = self._generate(
            [
                {"start": "abc", "end": 30, "name": "Bad"},
                {"start": 20, "end": 30, "name": "Fine"},
            ]
        )
        assert [s.name for s in result.segments] == ["Fine"]

    def test_overlap_trimmed(self):
        result = self._generate(
            [
                {"start": 10, "end": 30, "name": "A"},
                {"start": 25, "end": 50, "name": "B"},
            ]
        )
        assert result.segments[1].start == 30.0
        assert any("trimmed" in w for w in result.warnings)

    def test_max_segments_truncated(self):
        many = [{"start": i * 10, "end": i * 10 + 8, "name": f"S{i}"} for i in range(12)]
        result = self._generate(many, max_segments=3)
        assert len(result.segments) == 3

    def test_all_invalid_falls_back(self):
        result = self._generate([{"start": 200, "end": 300, "name": "Off the end"}])
        assert result.source == "fallback"

    def test_missing_name_gets_default(self):
        result = self._generate([{"start": 10, "end": 30}])
        assert result.segments[0].name == "Segment 1"


# ── Failure modes -> fallback ─────────────────────────────────────


class TestFallback:
    def test_malformed_json_falls_back(self):
        gen = make_generator(llm_returning("I think the best segments are {start: oops"))

        result = gen.generate(ANALYSIS, "x")

        assert result.source == "fallback"
        assert result.segments  # still produced something
        assert any("not valid JSON" in w for w in result.warnings)

    def test_wrong_shape_json_falls_back(self):
        gen = make_generator(llm_returning(json.dumps({"highlights": []})))
        assert gen.generate(ANALYSIS, "x").source == "fallback"

    def test_http_500_falls_back(self):
        def handler(request):
            return httpx.Response(500, json={"type": "error"})

        gen = make_generator(handler)
        result = gen.generate(ANALYSIS, "x")

        assert result.source == "fallback"
        assert any("LLM call failed" in w for w in result.warnings)

    def test_network_error_falls_back(self):
        def handler(request):
            raise httpx.ConnectError("boom")

        gen = make_generator(handler)
        assert gen.generate(ANALYSIS, "x").source == "fallback"

    def test_no_api_key_falls_back_without_calling(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        def handler(request):  # pragma: no cover - must never run
            raise AssertionError("API should not be called without a key")

        gen = SegmentGenerator(api_key="", client=httpx.Client(transport=httpx.MockTransport(handler)))
        result = gen.generate(ANALYSIS, "x")

        assert result.source == "fallback"

    def test_fallback_is_top_n_scenes_by_score_in_time_order(self):
        gen = SegmentGenerator(api_key="", max_segments=2)

        result = gen.generate(ANALYSIS, "x")

        # Top-2 by score: 45-90 (0.81) and 90-120 (0.53); emitted in time order.
        assert [(s.start, s.end) for s in result.segments] == [(45.0, 90.0), (90.0, 120.0)]
        assert [s.name for s in result.segments] == ["Highlight 1", "Highlight 2"]

    def test_fallback_degenerate_analysis_keeps_whole_video(self):
        gen = SegmentGenerator(api_key="")
        result = gen.generate({"duration": 60.0, "segments": []}, "x")

        assert result.source == "fallback"
        assert [(s.start, s.end) for s in result.segments] == [(0.0, 60.0)]


# ── EDL emission formats ──────────────────────────────────────────


class TestEdlFormats:
    SEGMENTS = [
        Segment(start=14.0, end=30.25, name="Opening rush"),
        Segment(start=45.0, end=90.0, name="Big play"),
    ]

    def test_csv_matches_upstream_import_format(self):
        csv_text = format_edl_csv(self.SEGMENTS)
        lines = csv_text.strip().split("\n")

        # Header matches edlFormats.ts csvHeader; rows are plain seconds.
        assert lines[0] == "Start,End,Name"
        assert lines[1] == "14,30.25,Opening rush"
        assert lines[2] == "45,90,Big play"

    def test_csv_quotes_names_with_commas(self):
        csv_text = format_edl_csv([Segment(0, 5, name="a, b")])
        assert '"a, b"' in csv_text

    def test_write_edl_csv(self, tmp_path):
        out = write_edl_csv(self.SEGMENTS, tmp_path / "vod.edl.csv")
        assert out.read_text(encoding="utf-8").startswith("Start,End,Name")

    def test_llc_matches_v2_schema(self):
        project = json.loads(format_llc_project(self.SEGMENTS, "vod.mp4"))

        assert project["version"] == 2
        assert project["mediaFileName"] == "vod.mp4"
        assert project["cutSegments"] == [
            {"start": 14.0, "end": 30.25, "name": "Opening rush"},
            {"start": 45.0, "end": 90.0, "name": "Big play"},
        ]
        # llcProjectV2Schema requires "name" on every segment.
        assert all("name" in s for s in project["cutSegments"])

    def test_sidecar_path_convention(self, tmp_path):
        """Sidecar must be '<stem>-proj.llc' next to the video (auto-load path)."""
        video = tmp_path / "MyVod.mp4"
        video.write_bytes(b"")

        sidecar = write_llc_sidecar(self.SEGMENTS, video)

        assert sidecar == tmp_path / "MyVod-proj.llc"
        assert json.loads(sidecar.read_text(encoding="utf-8"))["version"] == 2
