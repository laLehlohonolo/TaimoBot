"""Run twenty approved restore preflight final-fence checks sequentially."""
import importlib.util
import sys
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location(
    "approved_preflight_final_fence", ROOT / "tests" / "test_maintenance.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
CASES = sorted(
    name for name in dir(module.DailyMaintenanceTests)
    if name.startswith("test_approved_preflight_")
    and (name.endswith("_race") or "_race_" in name)
)


def main():
    suite = unittest.TestSuite(module.DailyMaintenanceTests(name) for name in CASES)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful() or result.testsRun != 20:
        print(f"APPROVED PREFLIGHT FINAL FENCE: {result.testsRun}/20")
        return 1
    print("TAIMOBOT APPROVED PREFLIGHT FINAL FENCE: 20/20 PASSED SEQUENTIALLY.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
