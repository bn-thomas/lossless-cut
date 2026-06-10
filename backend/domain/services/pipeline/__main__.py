"""``python -m backend.domain.services.pipeline`` entry point."""

import sys

from backend.domain.services.pipeline.cli import main

if __name__ == "__main__":
    sys.exit(main())
