"""Make ``solver`` resolve to *this* project's package.

When the tests are launched from the parent repository (``pytest solver-api/tests``),
the repository root is first on ``sys.path`` and would shadow this standalone
project with the older ``solver`` package that lives there.  Putting our own
directory in front keeps the test run self-contained.
"""

import sys
from pathlib import Path

HERE = str(Path(__file__).resolve().parents[1])
if HERE in sys.path:
    sys.path.remove(HERE)
sys.path.insert(0, HERE)