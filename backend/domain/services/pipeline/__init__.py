"""AI-assisted editing pipeline for the LosslessCut fork.

Modules:
  controller         — job-queue controller driving LosslessCut via LosslessCutService
  video_intelligence — pure-Python ffprobe/ffmpeg analysis (metadata, silence, scenes)
  segment_generator  — Claude-backed highlight selection + EDL emission
  orchestrator       — watch-folder state machine wiring the whole chain
  config             — env/args configuration for the CLI runner
"""

from backend.domain.services.pipeline.controller import (  # noqa: F401 — public API
    EditJob,
    JobStatus,
    PipelineController,
)
