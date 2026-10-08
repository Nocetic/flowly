#!/usr/bin/python3
"""Test-image-only override: product launcher and host Xvnc stay untouched."""

import os
import sys
from pathlib import Path

fps = Path("/tmp/flowly-benchmark-fps").read_text().strip()
if fps not in ("30", "60"):
    raise SystemExit("Invalid benchmark frame rate")
args = sys.argv[1:]
args[args.index("-FrameRate") + 1] = fps
os.execv("/usr/bin/Xtigervnc", ["/usr/bin/Xtigervnc", *args])
