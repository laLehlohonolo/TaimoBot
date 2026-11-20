"""Run twenty recovery-artifact inventory checks sequentially."""
import importlib.util
import sys
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location(
    "recovery_artifact_inventory", ROOT / "tests" / "test_maintenance.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

CASES = [
    "test_recovery_artifact_inventory_reports_empty_directory",
    "test_recovery_artifact_inventory_reports_valid_orphan_without_deleting",
    "test_recovery_artifact_inventory_matches_maintenance_run_references",
    "test_recovery_artifact_inventory_matches_failed_attempt_references",
    "test_recovery_artifact_inventory_matches_all_restore_plan_roles",
    "test_recovery_artifact_inventory_reports_missing_maintenance_run_file",
    "test_recovery_artifact_inventory_reports_missing_restore_plan_files",
    "test_recovery_artifact_inventory_detects_restore_plan_fingerprint_mismatch",
    "test_recovery_artifact_inventory_accepts_matching_restore_plan_fingerprints",
    "test_recovery_artifact_inventory_marks_corrupt_orphan_for_review",
    "test_recovery_artifact_inventory_marks_symlink_as_unsafe",
    "test_recovery_artifact_inventory_reports_temporary_files_as_incomplete",
    "test_recovery_artifact_inventory_detects_orphaned_staging_candidate",
    "test_recovery_artifact_inventory_blocks_invalid_execution_journal",
    "test_recovery_artifact_inventory_reports_journal_plan_without_database_plan",
    "test_recovery_artifact_inventory_blocks_incomplete_metadata_schema",
    "test_recovery_artifact_inventory_detects_hard_link_aliases",
    "test_recovery_artifact_inventory_detects_journal_append_during_scan",
    "test_recovery_artifact_inventory_protects_live_database_path",
    "test_recovery_artifact_inventory_does_not_mutate_files_database_or_journal",
]


def main():
    suite = unittest.TestSuite(module.DailyMaintenanceTests(name) for name in CASES)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful() or result.testsRun != 20:
        print(
            f"RECOVERY ARTIFACT INVENTORY: {result.testsRun}/20; "
            f"failures={len(result.failures)}, errors={len(result.errors)}"
        )
        return 1
    print("TAIMOBOT RECOVERY ARTIFACT INVENTORY: 20/20 PASSED SEQUENTIALLY.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
