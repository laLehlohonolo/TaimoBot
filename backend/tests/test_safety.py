import tempfile
import unittest
from pathlib import Path

from app import database
from app.database import init_db
from app.safety import (
    activate_emergency_stop,
    deactivate_emergency_stop,
    is_emergency_stop_active,
)


class PersistentEmergencyStopTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = database.DB_PATH
        database.DB_PATH = Path(self.temp_dir.name) / "safety.db"
        init_db()
        deactivate_emergency_stop()

    def tearDown(self):
        database.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def test_emergency_stop_survives_database_reinitialization(self):
        result = activate_emergency_stop()
        self.assertTrue(result["emergency_stop"])
        self.assertTrue(is_emergency_stop_active())

        # Re-running startup migrations must not silently clear the stop.
        init_db()
        self.assertTrue(is_emergency_stop_active())

        with database.get_connection() as connection:
            row = connection.execute(
                "SELECT emergency_stop_active FROM system_safety_state "
                "WHERE singleton_id = 1"
            ).fetchone()
        self.assertEqual(row["emergency_stop_active"], 1)

    def test_missing_safety_state_fails_closed_and_can_be_repaired(self):
        with database.get_connection() as connection:
            connection.execute("DELETE FROM system_safety_state")

        self.assertTrue(is_emergency_stop_active())
        result = deactivate_emergency_stop()
        self.assertFalse(result["emergency_stop"])
        self.assertFalse(is_emergency_stop_active())

    def test_state_is_visible_across_independent_database_connections(self):
        activate_emergency_stop()
        with database.get_connection() as first:
            first_value = first.execute(
                "SELECT emergency_stop_active FROM system_safety_state"
            ).fetchone()["emergency_stop_active"]
        with database.get_connection() as second:
            second_value = second.execute(
                "SELECT emergency_stop_active FROM system_safety_state"
            ).fetchone()["emergency_stop_active"]

        self.assertEqual(first_value, 1)
        self.assertEqual(second_value, 1)


if __name__ == "__main__":
    unittest.main()
