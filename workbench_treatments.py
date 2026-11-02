#!/usr/bin/env python3
"""Compatibility entry point; implementation lives in jev4mztabm.workbench_treatments."""

import sys
from pathlib import Path

# Keep source-checkout commands usable before installation.
sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
from jev4mztabm import workbench_treatments as _implementation

if __name__ == "__main__":
    raise SystemExit(_implementation.main())

sys.modules[__name__] = _implementation
