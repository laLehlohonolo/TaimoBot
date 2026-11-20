"""Run ten read-only restore recovery audit diagnostic checks sequentially."""
import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location("restore_audit_diagnostics", ROOT / "tests" / "test_maintenance.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

CASES = [
    "test_diagnostic_missing_audit_does_not_create_file",
    "test_diagnostic_empty_audit_reports_empty_without_mutation",
    "test_diagnostic_valid_completed_history_reports_consistent",
    "test_diagnostic_unmatched_started_matches_existing_marker",
    "test_diagnostic_unmatched_started_reports_missing_marker",
    "test_diagnostic_unmatched_started_never_confuses_replacement_marker",
    "test_diagnostic_malformed_or_partial_audit_is_invalid_and_unchanged",
    "test_diagnostic_rejects_symlink_without_touching_target",
    "test_diagnostic_returns_busy_when_writer_holds_exclusive_audit_lock",
    "test_diagnostic_never_changes_database_sidecars_marker_or_audit",
]

def main():
    suite = unittest.TestSuite(module.DailyMaintenanceTests(name) for name in CASES)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful() or result.testsRun != 10:
        print(f"RESTORE AUDIT DIAGNOSTICS: {result.testsRun}/10; failures={len(result.failures)}, errors={len(result.errors)}")
        return 1
    print("TAIMOBOT RESTORE AUDIT DIAGNOSTICS: 10/10 PASSED SEQUENTIALLY.")
    return 0

if __name__ == "__main__":
    sys.exit(main())
