import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import database
from app.database import init_db
from app.safety import deactivate_emergency_stop, require_trading_enabled
from app.tssi import activate_security_lock, get_security_status


class TSSISecurityInterlockTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = database.DB_PATH
        database.DB_PATH = Path(self.temp_dir.name) / "tssi.db"
        init_db()
        deactivate_emergency_stop()

    def tearDown(self):
        database.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def test_tssi_lock_blocks_entries_even_after_emergency_stop_resume(self):
        result = activate_security_lock(
            "Suspected integrity tampering",
            "tssi-test-event-001",
            "TEST_SECURITY_MONITOR",
        )
        self.assertTrue(result["tssi_lock_active"])

        # The ordinary emergency-stop resume path is not allowed to clear TSSI.
        deactivate_emergency_stop()
        status = get_security_status()
        self.assertTrue(status["tssi_lock_active"])
        self.assertEqual(status["lock_event_id"], "tssi-test-event-001")

        with patch("app.safety.is_maintenance_window", return_value=False):
            with self.assertRaisesRegex(RuntimeError, "blocked by TSSI"):
                require_trading_enabled()

    def test_missing_tssi_state_fails_closed(self):
        with database.get_connection() as connection:
            connection.execute("DELETE FROM tssi_security_state")

        status = get_security_status()
        self.assertTrue(status["tssi_lock_active"])
        self.assertEqual(status["tssi_status"], "STATE_MISSING_FAIL_CLOSED")

        with patch("app.safety.is_maintenance_window", return_value=False):
            with self.assertRaisesRegex(RuntimeError, "blocked by TSSI"):
                require_trading_enabled()

    def test_security_event_journal_is_append_only(self):
        activate_security_lock("Integrity alert", "tssi-test-event-002")
        with database.get_connection() as connection:
            with self.assertRaisesRegex(Exception, "append-only"):
                connection.execute(
                    "UPDATE tssi_security_events SET reason = 'rewritten' "
                    "WHERE event_id = 'tssi-test-event-002'"
                )
            with self.assertRaisesRegex(Exception, "append-only"):
                connection.execute(
                    "DELETE FROM tssi_security_events "
                    "WHERE event_id = 'tssi-test-event-002'"
                )


if __name__ == "__main__":
    unittest.main()
