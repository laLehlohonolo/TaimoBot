"""Run twenty recovery inventory filesystem-race checks sequentially."""
import importlib.util
import sys
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location(
    "recovery_inventory_filesystem_races", ROOT / "tests" / "test_maintenance.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

CASES = [
    "test_real_inventory_race_detects_file_append",
    "test_real_inventory_race_detects_atomic_file_replacement",
    "test_real_inventory_race_blocks_file_deletion",
    "test_real_inventory_race_blocks_file_rename",
    "test_real_inventory_race_blocks_root_rename",
    "test_real_inventory_race_blocks_root_symlink_swap",
    "test_real_inventory_race_blocks_root_regular_file_swap",
    "test_real_inventory_race_blocks_new_backup_entry",
    "test_real_inventory_race_blocks_removed_backup_entry",
    "test_real_inventory_race_blocks_staging_symlink_swap",
    "test_real_inventory_race_blocks_staging_directory_removal",
    "test_real_inventory_race_blocks_staging_directory_creation",
    "test_real_inventory_race_detects_staged_file_append",
    "test_real_inventory_race_blocks_staged_file_deletion",
    "test_real_inventory_race_blocks_staged_file_symlink_swap",
    "test_real_inventory_race_blocks_new_staging_entry",
    "test_real_inventory_race_detects_file_timestamp_change",
    "test_real_inventory_race_detects_staged_timestamp_change",
    "test_real_inventory_race_recovers_after_transient_root_swap",
    "test_real_inventory_race_preserves_external_directory",
]


def main():
    suite = unittest.TestSuite(module.DailyMaintenanceTests(name) for name in CASES)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful() or result.testsRun != 20:
        print(f"RECOVERY INVENTORY FILESYSTEM RACES: {result.testsRun}/20; failures={len(result.failures)}, errors={len(result.errors)}")
        return 1
    print("TAIMOBOT RECOVERY INVENTORY FILESYSTEM RACES: 20/20 PASSED SEQUENTIALLY.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
