"""Run twenty-four invalid restore-lock timestamp checks sequentially."""
import importlib.util
import sys
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location(
    "restore_lock_timestamp_validation", ROOT / "tests" / "test_maintenance.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
CASES = sorted(
    name for name in dir(module.DailyMaintenanceTests)
    if name.startswith("test_restore_lock_invalid_timestamp_")
)


def main():
    suite = unittest.TestSuite(module.DailyMaintenanceTests(name) for name in CASES)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful() or result.testsRun != 24:
        print(f"RESTORE LOCK TIMESTAMP VALIDATION: {result.testsRun}/24")
        return 1
    print("TAIMOBOT RESTORE LOCK TIMESTAMP VALIDATION: 24/24 PASSED SEQUENTIALLY.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
