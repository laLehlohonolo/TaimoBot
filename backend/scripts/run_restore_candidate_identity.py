"""Run ten exact-candidate restore identification checks sequentially."""
import importlib.util
import sys
import unittest
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))
TEST_FILE = BACKEND_ROOT / "tests" / "test_maintenance.py"
SPEC = importlib.util.spec_from_file_location("restore_candidate_identity_tests", TEST_FILE)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

CASES = [
    "test_restore_preparation_selects_exact_candidate_from_multiple_valid_backups",
    "test_restore_preparation_does_not_fallback_when_named_candidate_is_missing",
    "test_restore_preparation_rejects_inventory_candidate_not_restore_ready",
    "test_restore_preparation_rejects_path_traversal_backup_name",
    "test_restore_preparation_rejects_symlinked_selected_backup",
    "test_restore_preparation_rejects_symlink_swap_after_inventory",
    "test_restore_preparation_rejects_regular_file_replacement_after_inventory",
    "test_restore_preparation_requires_inventory_fingerprint",
    "test_restore_preparation_rechecks_source_fingerprint_after_staging",
    "test_restore_preparation_persists_operator_and_exact_candidate_fingerprint",
]


def main():
    suite = unittest.TestSuite()
    for name in CASES:
        suite.addTest(MODULE.DailyMaintenanceTests(name))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful() or result.testsRun != 10:
        print(f"RESTORE CANDIDATE IDENTITY: {result.testsRun}/10 executed; failures={len(result.failures)}, errors={len(result.errors)}")
        return 1
    print("TAIMOBOT RESTORE CANDIDATE IDENTITY: 10/10 PASSED SEQUENTIALLY.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
