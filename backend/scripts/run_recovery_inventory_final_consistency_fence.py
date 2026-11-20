"""Run twenty final consistency-fence race checks sequentially."""
import importlib.util
import sys
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location(
    "recovery_inventory_final_consistency_fence",
    ROOT / "tests" / "test_maintenance.py",
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
CASES = [name for name, _scenario in module._REAL_INVENTORY_FINAL_CONSISTENCY_FENCE_CASES]


def main():
    suite = unittest.TestSuite(module.DailyMaintenanceTests(name) for name in CASES)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful() or result.testsRun != 20:
        print(f"RECOVERY INVENTORY FINAL CONSISTENCY FENCE: {result.testsRun}/20")
        return 1
    print("TAIMOBOT RECOVERY INVENTORY FINAL CONSISTENCY FENCE: 20/20 PASSED SEQUENTIALLY.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
