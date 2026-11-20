"""Run ten restore recovery audit fsync-durability checks sequentially."""
import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location("restore_audit_fsync_boundaries", ROOT / "tests" / "test_maintenance.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

CASES = [
    "test_started_fsync_failure_keeps_marker_and_unmatched_audit",
    "test_started_fsync_failure_blocks_retry_without_audit_mutation",
    "test_started_fsync_precedes_marker_unlink",
    "test_marker_directory_fsync_precedes_complete_append",
    "test_complete_fsync_is_final_barrier_before_success_return",
    "test_started_fsync_failure_preserves_database_and_sidecars",
    "test_complete_fsync_failure_preserves_database_and_sidecars",
    "test_complete_fsync_failure_never_returns_success",
    "test_complete_fsync_failure_cannot_be_automatically_retried_or_repaired",
    "test_unsynced_complete_record_loss_is_detected_as_unmatched_started",
]

def main():
    suite = unittest.TestSuite(module.DailyMaintenanceTests(name) for name in CASES)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful() or result.testsRun != 10:
        print(f"RESTORE AUDIT FSYNC DURABILITY: {result.testsRun}/10; failures={len(result.failures)}, errors={len(result.errors)}")
        return 1
    print("TAIMOBOT RESTORE AUDIT FSYNC DURABILITY: 10/10 PASSED SEQUENTIALLY.")
    return 0

if __name__ == "__main__":
    sys.exit(main())
