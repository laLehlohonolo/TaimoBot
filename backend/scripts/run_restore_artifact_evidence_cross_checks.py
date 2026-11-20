"""Run ten restore journal-to-artifact evidence checks sequentially."""
import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location("restore_artifact_cross_checks", ROOT / "tests" / "test_maintenance.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

CASES = [
    "test_artifact_cross_check_rejects_staged_hash_disagreement_at_publish",
    "test_artifact_cross_check_rejects_published_hash_vs_postcheck_disagreement",
    "test_artifact_cross_check_rejects_rollback_start_backup_disagreement",
    "test_artifact_cross_check_rejects_rollback_verified_hash_vs_backup",
    "test_artifact_cross_check_rejects_same_path_for_multiple_roles",
    "test_artifact_cross_check_rejects_hard_link_aliases",
    "test_artifact_cross_check_blocks_semantically_invalid_plan_token_reuse",
    "test_artifact_cross_check_detects_journal_append_during_file_inspection",
    "test_artifact_cross_check_detects_sidecar_created_during_integrity_inspection",
    "test_artifact_cross_check_preserves_all_files_and_journal_on_conflict",
]

def main():
    suite = unittest.TestSuite(module.DailyMaintenanceTests(name) for name in CASES)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful() or result.testsRun != 10:
        print(f"RESTORE ARTIFACT EVIDENCE CROSS-CHECKS: {result.testsRun}/10; failures={len(result.failures)}, errors={len(result.errors)}")
        return 1
    print("TAIMOBOT RESTORE ARTIFACT EVIDENCE CROSS-CHECKS: 10/10 PASSED SEQUENTIALLY.")
    return 0

if __name__ == "__main__":
    sys.exit(main())
