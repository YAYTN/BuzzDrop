#!/usr/bin/env python3
"""Run BuzzDrop straight from the source folder (double-click on Windows,
or ``python3 BuzzDrop.pyw`` anywhere). No packages to install."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from buzzdrop.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
