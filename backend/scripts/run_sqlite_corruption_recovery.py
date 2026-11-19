"""Run ten byte-level database corruption and restore-identification checks sequentially."""
import importlib.util
import sys
import unittest
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))
TEST_FILE = BACKEND_ROOT / "tests" / "test_maintenance.py"
SPEC = importlib.util.spec_from_file_location("sqlite_corruption_recovery_tests", TEST_FILE)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

CASES = [
    "test_byte_corrupted_sqlite_backup_is_not_restore_ready",
    "test_byte_corrupted_source_backup_blocks_restore_preflight",
    "test_byte_corrupted_staged_candidate_blocks_restore_preflight",
    "test_byte_corrupted_rollback_backup_blocks_restore_preflight",
    "test_byte_corrupted_live_database_blocks_restore_preflight_without_replacement",
    "test_restore_candidate_verification_rejects_page_corruption_and_cleans_staging",
    "test_inventory_identifies_exact_valid_candidate_among_corrupt_neighbours",
    "test_byte_corrupted_candidate_with_wal_sidecar_is_skipped_and_preserved",
    "test_byte_corruption_after_inventory_is_rejected_by_final_preflight",
    "test_corrupt_restore_candidate_never_changes_live_database_fingerprint",
]


def main():
    suite = unittest.TestSuite()
    for name in CASES:
        suite.addTest(MODULE.DailyMaintenanceTests(name))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful() or result.testsRun != 10:
        print(f"SQLITE CORRUPTION RECOVERY GROUP: {result.testsRun}/10 executed; failures={len(result.failures)}, errors={len(result.errors)}")
        return 1
    print("TAIMOBOT SQLITE CORRUPTION RECOVERY: 10/10 PASSED SEQUENTIALLY.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
