"""Run ten restore journal append identity checks sequentially."""
import importlib.util
import sys
import unittest
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))
TEST_FILE = BACKEND_ROOT / "tests" / "test_maintenance.py"
SPEC = importlib.util.spec_from_file_location("restore_journal_identity_tests", TEST_FILE)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

CASES = [
    "test_journal_append_rejects_regular_path_replacement_during_verification",
    "test_journal_append_rejects_symlink_swap_during_verification",
    "test_journal_append_rejects_path_deletion_during_verification",
    "test_journal_append_rejects_in_place_write_during_verification",
    "test_journal_append_rejects_non_regular_directory_path",
    "test_journal_append_continues_hash_chain_after_identity_checked_verification",
    "test_journal_append_refuses_corrupt_existing_chain_without_modifying_bytes",
    "test_journal_append_refuses_symlink_before_open",
    "test_journal_append_refuses_oversized_event_without_appending",
    "test_journal_append_rejects_invalid_event_without_creating_file",
]


def main():
    suite = unittest.TestSuite()
    for name in CASES:
        suite.addTest(MODULE.DailyMaintenanceTests(name))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful() or result.testsRun != 10:
        print(f"RESTORE JOURNAL IDENTITY: {result.testsRun}/10 executed; failures={len(result.failures)}, errors={len(result.errors)}")
        return 1
    print("TAIMOBOT RESTORE JOURNAL IDENTITY: 10/10 PASSED SEQUENTIALLY.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
