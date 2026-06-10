"""CLI entry point for the watch-folder pipeline.

Run from the workspace root:

    python -m backend.domain.services.pipeline --watch-dir /path/to/inbox \\
        --intent "pull the best fight scenes" --once

Configuration precedence: CLI args > environment variables > defaults.

Environment variables:
  PIPELINE_WATCH_DIR       watch folder (required unless --watch-dir given)
  PIPELINE_ARCHIVE_DIR     processed-files dir (default <watch>/archive)
  PIPELINE_FAILED_DIR      failed-files dir (default <watch>/failed)
  PIPELINE_INTENT          highlight-selection intent string
  PIPELINE_LLM_MODEL       Anthropic model id (default claude-fable-5)
  PIPELINE_POLL_INTERVAL   seconds between folder polls (default 10)
  ANTHROPIC_API_KEY        enables LLM selection (heuristic fallback if unset)
  LOSSLESSCUT_PATH         explicit LosslessCut executable path
  LOSSLESSCUT_PORT         HTTP API port (default 8080)
"""

import argparse
import logging
import os
import sys
from typing import Optional

from backend.domain.services.losslesscut_service import LosslessCutService
from backend.domain.services.pipeline.controller import PipelineController
from backend.domain.services.pipeline.orchestrator import (
    OrchestratorConfig,
    WatchFolderOrchestrator,
)
from backend.domain.services.pipeline.segment_generator import SegmentGenerator
from backend.domain.services.pipeline.video_intelligence import VideoIntelligence

logger = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    env = os.environ
    parser = argparse.ArgumentParser(
        prog="python -m backend.domain.services.pipeline",
        description="Watch a folder; turn each new video into highlight clips via LosslessCut.",
    )
    parser.add_argument(
        "--watch-dir",
        default=env.get("PIPELINE_WATCH_DIR"),
        help="Folder to watch for new videos (env: PIPELINE_WATCH_DIR)",
    )
    parser.add_argument(
        "--archive-dir",
        default=env.get("PIPELINE_ARCHIVE_DIR"),
        help="Where processed videos move (default: <watch>/archive)",
    )
    parser.add_argument(
        "--failed-dir",
        default=env.get("PIPELINE_FAILED_DIR"),
        help="Where failed videos move (default: <watch>/failed)",
    )
    parser.add_argument(
        "--intent",
        default=env.get("PIPELINE_INTENT", "Extract the most interesting highlight moments"),
        help="What the highlights should capture (env: PIPELINE_INTENT)",
    )
    parser.add_argument(
        "--model",
        default=env.get("PIPELINE_LLM_MODEL"),
        help="Anthropic model id (default: claude-fable-5; env: PIPELINE_LLM_MODEL)",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=float(env.get("PIPELINE_POLL_INTERVAL", "10")),
        help="Seconds between folder polls (env: PIPELINE_POLL_INTERVAL)",
    )
    parser.add_argument(
        "--max-segments",
        type=int,
        default=int(env.get("PIPELINE_MAX_SEGMENTS", "8")),
        help="Maximum highlight segments per video",
    )
    parser.add_argument(
        "--losslesscut-path",
        default=env.get("LOSSLESSCUT_PATH"),
        help="Explicit LosslessCut executable (env: LOSSLESSCUT_PATH)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(env.get("LOSSLESSCUT_PORT", "8080")),
        help="LosslessCut HTTP API port (env: LOSSLESSCUT_PORT)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single poll pass and exit (instead of looping forever)",
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="Debug logging")
    return parser


def build_orchestrator(args: argparse.Namespace) -> WatchFolderOrchestrator:
    service = LosslessCutService(
        http_port=args.port,
        executable_path=args.losslesscut_path,
    )
    controller = PipelineController(service=service)
    generator = SegmentGenerator(model=args.model, max_segments=args.max_segments)
    config = OrchestratorConfig(
        watch_dir=args.watch_dir,
        archive_dir=args.archive_dir,
        failed_dir=args.failed_dir,
        intent=args.intent,
        poll_interval=args.poll_interval,
    )
    return WatchFolderOrchestrator(
        config=config,
        intelligence=VideoIntelligence(),
        generator=generator,
        controller=controller,
    )


def main(argv: Optional[list] = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if not args.watch_dir:
        print(
            "error: --watch-dir (or PIPELINE_WATCH_DIR) is required",
            file=sys.stderr,
        )
        return 2

    if not os.environ.get("ANTHROPIC_API_KEY"):
        logger.warning(
            "ANTHROPIC_API_KEY not set — segment selection will use the heuristic fallback instead of Claude"
        )

    orchestrator = build_orchestrator(args)
    if args.once:
        jobs = orchestrator.run_once()
        orchestrator.controller.shutdown()
        failed = [j for j in jobs if j.error]
        logger.info("Processed %d file(s), %d failed", len(jobs), len(failed))
        return 1 if failed else 0

    orchestrator.run_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
