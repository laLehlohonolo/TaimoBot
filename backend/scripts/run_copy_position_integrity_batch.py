"""Run thirty copy and position-group integrity checks sequentially as one batch."""
import importlib.util
import sys
import unittest
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))


def load_module(module_name, relative_path):
    test_file = BACKEND_ROOT / relative_path
    spec = importlib.util.spec_from_file_location(module_name, test_file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


copy_module = load_module(
    "copy_integrity_regression_tests",
    Path("tests") / "test_copy_replication.py",
)
position_module = load_module(
    "position_group_integrity_regression_tests",
    Path("tests") / "test_position_groups.py",
)

COPY_RECOVERY_CASES = [
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

COPY_SAFETY_CASES = [
    "test_follower_position_size_is_calculated_from_own_funding",
    "test_concurrent_replication_of_same_source_creates_one_follower_trade",
    "test_paused_relationship_receives_no_copy",
    "test_copied_trade_cannot_trigger_cascading_copy",
    "test_source_trade_must_be_open",
    "test_unavailable_follower_account_records_failure_without_affecting_source",
    "test_follower_risk_rejection_is_recorded_without_affecting_source",
    "test_emergency_stop_blocks_copy_execution_without_affecting_source",
    "test_maintenance_blocks_copy_execution_without_affecting_source",
    "test_concurrent_copy_entries_cannot_exceed_followers_combined_risk",
]

POSITION_GROUP_CASES = [
    "test_group_reports_combined_position_statistics",
    "test_group_cannot_exceed_account_risk_limit",
    "test_group_close_closes_all_positions_atomically",
    "test_group_close_closes_source_and_follower_copies",
    "test_group_close_allocates_costs_to_sources_once_not_follower_copies",
    "test_group_close_rolls_back_when_follower_close_fails",
    "test_group_close_fails_uncommitted_pending_copy_and_releases_lease",
    "test_group_close_reconciles_pending_execution_for_closed_follower",
    "test_group_close_rolls_back_source_balance_if_follower_balance_write_fails",
    "test_group_close_rolls_back_when_final_group_status_write_fails",
]


def main():
    suite = unittest.TestSuite()
    suite.addTests(
        copy_module.CopyTradeReplicationTests(name)
        for name in COPY_RECOVERY_CASES + COPY_SAFETY_CASES
    )
    suite.addTests(
        position_module.PositionGroupTests(name)
        for name in POSITION_GROUP_CASES
    )
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful() or result.testsRun != 30:
        print(
            f"COPY/POSITION INTEGRITY BATCH: {result.testsRun}/30 executed; "
            f"failures={len(result.failures)}, errors={len(result.errors)}"
        )
        return 1
    print("TAIMOBOT COPY/POSITION INTEGRITY BATCH: 30/30 PASSED SEQUENTIALLY.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
