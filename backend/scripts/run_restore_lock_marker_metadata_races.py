"""Run twenty restore-lock marker metadata race checks sequentially."""
import importlib.util
import sys
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location(
    "restore_lock_marker_metadata_races", ROOT / "tests" / "test_maintenance.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
CASES = sorted(
    name for name in dir(module.DailyMaintenanceTests)
    if name.startswith("test_restore_lock_marker_metadata_race_")
)


def main():
    suite = unittest.TestSuite(module.DailyMaintenanceTests(name) for name in CASES)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful() or result.testsRun != 20:
        print(f"RESTORE LOCK MARKER METADATA RACES: {result.testsRun}/20")
        return 1
    print("TAIMOBOT RESTORE LOCK MARKER METADATA RACES: 20/20 PASSED SEQUENTIALLY.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
