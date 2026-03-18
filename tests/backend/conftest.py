"""Backend test configuration.

Shared backend test helpers live here. Runtime workflow-routing defaults are no
longer overridden globally because selector compatibility env toggles were
removed from production code.
"""

import os
import sys
from pathlib import Path

_BACKEND_TESTS_DIR = Path(__file__).resolve().parent
if str(_BACKEND_TESTS_DIR) not in sys.path:
    # Keep backend-shared test helpers importable without turning tests into a package.
    sys.path.insert(0, str(_BACKEND_TESTS_DIR))
