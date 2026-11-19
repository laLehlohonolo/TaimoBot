"""Run the ten copy-recovery regression cases sequentially as a named group."""
import importlib.util
import sys
from pathlib import Path
import unittest

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))
TEST_FILE = BACKEND_ROOT / "tests" / "test_copy_replication.py"
SPEC = importlib.util.spec_from_file_location("copy_replication_tests", TEST_FILE)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

CASES = [
    "test_worker_crash_after_follower_insert_is_recovered_without_duplicate",
    "test_pending_copy_recovers_existing_committed_follower_trade",
    "test_recent_pending_copy_is_not_stolen_from_active_worker",
    "test_expired_lease_reclaim_rejects_old_worker_token",
    "test_two_workers_racing_to_reclaim_expired_lease_have_one_owner",
    "test_paused_or_stopped_relationship_finalizes_pending_without_opening_copy",
    "test_restart_reconciles_pending_copy_after_source_and_follower_closed",
    "test_source_close_during_copy_execution_keeps_follower_and_execution_closed",
    "test_source_closed_before_atomic_copy_insert_prevents_follower_entry",
    "test_paused_relationship_before_atomic_copy_insert_prevents_follower_entry",
]


def main():
    suite = unittest.TestSuite()
    for name in CASES:
        suite.addTest(MODULE.CopyTradeReplicationTests(name))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful() or result.testsRun != 10:
        print(f"COPY RECOVERY GROUP: {result.testsRun}/10 executed; failures={len(result.failures)}, errors={len(result.errors)}")
        return 1
    print("TAIMOBOT COPY RECOVERY GROUP: 10/10 PASSED SEQUENTIALLY.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
