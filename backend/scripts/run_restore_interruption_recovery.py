"""Run ten restore-lock recovery audit checks sequentially."""
import importlib.util
import sys
import unittest
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))
TEST_FILE = BACKEND_ROOT / "tests" / "test_maintenance.py"
SPEC = importlib.util.spec_from_file_location("restore_lock_recovery_audit_tests", TEST_FILE)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

CASES = [
    "test_recovery_audit_success_writes_verifiable_hash_chain",
    "test_recovery_audit_chains_a_later_recovery_after_prior_complete",
    "test_recovery_refuses_audit_record_tampering_without_removing_marker",
    "test_recovery_refuses_incomplete_audit_tail_without_removing_marker",
    "test_recovery_refuses_duplicate_json_keys_in_audit",
    "test_recovery_refuses_broken_audit_hash_chain",
    "test_recovery_refuses_audit_claiming_database_mutation",
    "test_recovery_refuses_unknown_audit_event",
    "test_recovery_refuses_oversized_audit_before_appending",
    "test_recovery_audit_write_failure_preserves_abandoned_marker",
]


def main():
    suite = unittest.TestSuite()
    for name in CASES:
        suite.addTest(MODULE.DailyMaintenanceTests(name))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful() or result.testsRun != 10:
        print(f"RESTORE LOCK RECOVERY AUDIT: {result.testsRun}/10 executed; failures={len(result.failures)}, errors={len(result.errors)}")
        return 1
    print("TAIMOBOT RESTORE LOCK RECOVERY AUDIT: 10/10 PASSED SEQUENTIALLY.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
