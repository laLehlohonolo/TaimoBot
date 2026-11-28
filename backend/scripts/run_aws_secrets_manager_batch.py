"""Run the focused AWS Secrets Manager adapter contract tests sequentially."""
from pathlib import Path
import sys
import unittest

# The workflow invokes this script from backend/scripts. Put the backend root
# on sys.path explicitly so both app.* and test imports resolve consistently.
BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.discover(
        start_dir=str(BACKEND_ROOT / "tests"),
        pattern="test_aws_secrets_manager.py",
        top_level_dir=str(BACKEND_ROOT / "tests"),
    )
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(0 if result.wasSuccessful() else 1)
