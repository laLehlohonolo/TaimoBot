"""Run twenty live-database identity race checks sequentially."""
import importlib.util
import sys
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location(
    "recovery_inventory_database_identity_races",
    ROOT / "tests" / "test_maintenance.py",
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
CASES = [name for name, _scenario in module._REAL_INVENTORY_DATABASE_IDENTITY_RACE_CASES]


def main():
    suite = unittest.TestSuite(module.DailyMaintenanceTests(name) for name in CASES)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful() or result.testsRun != 20:
        print(
            f"RECOVERY INVENTORY DATABASE IDENTITY RACES: {result.testsRun}/20; "
            f"failures={len(result.failures)}, errors={len(result.errors)}"
        )
        return 1
    print("TAIMOBOT RECOVERY INVENTORY DATABASE IDENTITY RACES: 20/20 PASSED SEQUENTIALLY.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
