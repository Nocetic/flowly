"""Global test isolation: unit tests must never download model assets."""

from __future__ import annotations

import os

os.environ.setdefault("FLOWLY_SEMANTIC_MODEL_DOWNLOAD", "0")
