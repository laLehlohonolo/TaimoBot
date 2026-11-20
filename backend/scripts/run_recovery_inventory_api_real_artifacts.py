"""Run twenty real-file recovery inventory API checks sequentially."""
import importlib.util
import sys
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location(
    "recovery_inventory_real_api", ROOT / "tests" / "test_maintenance.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

CASES = [
    "test_real_inventory_api_reports_actual_orphan_backup",
    "test_real_inventory_api_recognizes_completed_maintenance_reference",
    "test_real_inventory_api_reports_missing_completed_backup",
    "test_real_inventory_api_recognizes_failed_attempt_backup",
    "test_real_inventory_api_recognizes_superseded_attempt_backup",
    "test_real_inventory_api_recognizes_all_prepared_restore_artifact_roles",
    "test_real_inventory_api_reports_missing_staged_candidate",
    "test_real_inventory_api_reports_missing_rollback_backup",
    "test_real_inventory_api_detects_changed_restore_source_fingerprint",
    "test_real_inventory_api_classifies_corrupt_sqlite_as_review_not_orphan",
    "test_real_inventory_api_classifies_symlink_as_unsafe_without_following",
    "test_real_inventory_api_detects_hardlink_alias_identity",
    "test_real_inventory_api_reports_orphaned_staging_candidate",
    "test_real_inventory_api_reports_incomplete_temporary_artifact",
    "test_real_inventory_api_blocks_invalid_restore_journal",
    "test_real_inventory_api_orders_artifacts_deterministically",
    "test_real_inventory_api_protects_live_database_when_inside_backup_root",
    "test_real_inventory_api_repeated_requests_preserve_database_backup_and_journal",
    "test_real_inventory_api_flags_reference_outside_managed_root",
    "test_real_inventory_api_keeps_orphan_and_issue_classification_separate",
]


def main():
    suite = unittest.TestSuite(module.DailyMaintenanceTests(name) for name in CASES)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful() or result.testsRun != 20:
        print(f"REAL-ARTIFACT RECOVERY INVENTORY API: {result.testsRun}/20; failures={len(result.failures)}, errors={len(result.errors)}")
        return 1
    print("TAIMOBOT REAL-ARTIFACT RECOVERY INVENTORY API: 20/20 PASSED SEQUENTIALLY.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
