"""Run managed-directory metadata race checks sequentially."""
import importlib.util
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
spec = importlib.util.spec_from_file_location("test_maintenance", ROOT / "backend/tests/test_maintenance.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
names = [name for name, _, _ in module._MANAGED_DIRECTORY_METADATA_RACE_CASES]
suite = unittest.TestSuite(module.DailyMaintenanceTests(name) for name in names)
result = unittest.TextTestRunner(verbosity=2).run(suite)
if not result.wasSuccessful() or result.testsRun != 20:
    raise SystemExit(f"MANAGED DIRECTORY METADATA RACES FAILED: {result.testsRun}/20")
print("TAIMOBOT MANAGED DIRECTORY METADATA RACES: 20/20 PASSED SEQUENTIALLY.")
