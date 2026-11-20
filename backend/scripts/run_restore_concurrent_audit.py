"""Run ten concurrent restore audit append trials sequentially."""
import importlib.util
import sys
import unittest
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))
TEST_FILE = BACKEND_ROOT / "tests" / "test_maintenance.py"
SPEC = importlib.util.spec_from_file_location("restore_concurrent_audit", TEST_FILE)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

CASES = [f"test_concurrent_recovery_audit_appends_trial_{index:02d}" for index in range(1, 11)]


def main():
    suite = unittest.TestSuite()
    for name in CASES:
        suite.addTest(MODULE.DailyMaintenanceTests(name))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful() or result.testsRun != 10:
        print(f"RESTORE CONCURRENT AUDIT: {result.testsRun}/10; failures={len(result.failures)}, errors={len(result.errors)}")
        return 1
    print("TAIMOBOT RESTORE CONCURRENT AUDIT: 10/10 PASSED SEQUENTIALLY.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
