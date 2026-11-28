"""Run the focused AWS Secrets Manager adapter contract tests sequentially."""
import unittest


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.discover(
        start_dir="tests",
        pattern="test_aws_secrets_manager.py",
    )
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(0 if result.wasSuccessful() else 1)
