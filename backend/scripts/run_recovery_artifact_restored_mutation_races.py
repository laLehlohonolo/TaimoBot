"""Run twenty transiently restored file-mutation checks sequentially."""
import importlib.util
import sys
from pathlib import Path
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
spec=importlib.util.spec_from_file_location("restored_mutation_tests",ROOT/"tests"/"test_maintenance.py")
module=importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
CASES=sorted(name for name in dir(module.RecoveryArtifactRestoredMutationRaceTests) if name.startswith("test_transient_"))

def main():
    suite=unittest.TestSuite(module.RecoveryArtifactRestoredMutationRaceTests(name) for name in CASES)
    result=unittest.TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful() or result.testsRun!=20:
        print(f"TRANSIENTLY RESTORED ARTIFACT MUTATIONS: {result.testsRun}/20")
        return 1
    print("TAIMOBOT TRANSIENTLY RESTORED ARTIFACT MUTATIONS: 20/20 PASSED SEQUENTIALLY.")
    return 0

if __name__=="__main__":
    sys.exit(main())
