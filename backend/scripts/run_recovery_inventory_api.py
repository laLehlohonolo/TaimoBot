"""Run twenty recovery inventory API checks sequentially."""
import importlib.util
import sys
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location(
    "recovery_inventory_api", ROOT / "tests" / "test_maintenance.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

CASES = [
    "test_recovery_inventory_api_requires_authentication",
    "test_recovery_inventory_api_rejects_invalid_bearer_token",
    "test_recovery_inventory_api_rejects_non_admin_user",
    "test_recovery_inventory_api_requires_explicit_admin_allowlist",
    "test_recovery_inventory_api_normalizes_admin_email_case",
    "test_recovery_inventory_api_calls_inventory_once_for_admin",
    "test_recovery_inventory_api_does_not_call_inventory_for_non_admin",
    "test_recovery_inventory_api_returns_inventory_status",
    "test_recovery_inventory_api_preserves_orphan_details",
    "test_recovery_inventory_api_preserves_missing_reference_details",
    "test_recovery_inventory_api_preserves_diagnostic_issues",
    "test_recovery_inventory_api_exposes_read_only_contract",
    "test_recovery_inventory_api_does_not_expose_restore_execution_controls",
    "test_recovery_inventory_api_does_not_trigger_backup_creation",
    "test_recovery_inventory_api_does_not_trigger_backup_cleanup",
    "test_recovery_inventory_api_does_not_trigger_restore_recovery",
    "test_recovery_inventory_api_repeated_gets_are_read_only",
    "test_recovery_inventory_api_rejects_post_method",
    "test_recovery_inventory_api_returns_blocked_diagnostic_without_mutation",
    "test_recovery_inventory_api_does_not_change_maintenance_database",
]


def main():
    suite = unittest.TestSuite(module.DailyMaintenanceTests(name) for name in CASES)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful() or result.testsRun != 20:
        print(
            f"RECOVERY INVENTORY API: {result.testsRun}/20; "
            f"failures={len(result.failures)}, errors={len(result.errors)}"
        )
        return 1
    print("TAIMOBOT RECOVERY INVENTORY API: 20/20 PASSED SEQUENTIALLY.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
