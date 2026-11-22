import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import database
from app.database import init_db
from app.main import app


class SubscriptionEntitlementTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = database.DB_PATH
        database.DB_PATH = Path(self.temp_dir.name) / "subscription-test.db"
        init_db()
        self.client = TestClient(app)
        self.admin_env = patch.dict(
            os.environ,
            {"TAIMOBOT_ADMIN_EMAILS": "admin@taimobot.local"},
        )
        self.admin_env.start()
        self.admin_headers = self._register_and_login(
            "admin@taimobot.local", "AdminPassphrase-2026!"
        )
        self.participant_headers = self._register_and_login(
            "participant@taimobot.local", "ParticipantPassphrase-2026!"
        )

    def tearDown(self):
        self.client.close()
        self.admin_env.stop()
        database.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def _register_and_login(self, email, password):
        registered = self.client.post(
            "/auth/register",
            json={"email": email, "password": password},
        )
        self.assertEqual(registered.status_code, 200, registered.text)
        logged_in = self.client.post(
            "/auth/login",
            json={"email": email, "password": password},
        )
        self.assertEqual(logged_in.status_code, 200, logged_in.text)
        return {"Authorization": "Bearer " + logged_in.json()["access_token"]}

    def _assign_tier(self, tier):
        me = self.client.get("/auth/me", headers=self.participant_headers).json()
        response = self.client.put(
            f"/auth/admin/participants/{me['user_id']}/subscription",
            headers=self.admin_headers,
            json={"tier": tier, "reason": "Automated entitlement boundary test"},
        )
        self.assertEqual(response.status_code, 200, response.text)

    def _create_account(self, account_id):
        return self.client.post(
            "/accounts",
            headers=self.participant_headers,
            json={
                "account_id": account_id,
                "starting_balance": 1000,
                "settlement_currency": "ZAR",
            },
        )

    def test_all_four_tier_account_limits_are_enforced(self):
        for tier, limit in (
            ("STANDARD", 1),
            ("ADVANCED", 3),
            ("PREMIUM", 5),
            ("EXCLUSIVE", 10),
        ):
            with self.subTest(tier=tier):
                # Each tier boundary uses a distinct participant identity.
                email = f"{tier.lower()}@taimobot.local"
                headers = self._register_and_login(
                    email, "ParticipantPassphrase-2026!"
                )
                original_headers = self.participant_headers
                self.participant_headers = headers
                try:
                    self._assign_tier(tier)
                    for index in range(limit):
                        created = self._create_account(f"{tier}-{index}")
                        self.assertEqual(created.status_code, 200, created.text)
                    blocked = self._create_account(f"{tier}-over-limit")
                    self.assertEqual(blocked.status_code, 400, blocked.text)
                    self.assertIn(str(limit), blocked.json()["detail"])
                    entitlement = self.client.get(
                        "/auth/subscription/entitlement",
                        headers=headers,
                    )
                    self.assertEqual(entitlement.status_code, 200)
                    self.assertEqual(entitlement.json()["tier"], tier)
                    self.assertEqual(entitlement.json()["maximum_accounts"], limit)
                    self.assertEqual(entitlement.json()["accounts_used"], limit)
                    self.assertEqual(entitlement.json()["accounts_remaining"], 0)
                finally:
                    self.participant_headers = original_headers

    def test_participant_cannot_assign_their_own_subscription_tier(self):
        me = self.client.get(
            "/auth/me", headers=self.participant_headers
        ).json()
        response = self.client.put(
            f"/auth/admin/participants/{me['user_id']}/subscription",
            headers=self.participant_headers,
            json={"tier": "EXCLUSIVE", "reason": "Attempted self-upgrade"},
        )
        self.assertEqual(response.status_code, 403)

        entitlement = self.client.get(
            "/auth/subscription/entitlement",
            headers=self.participant_headers,
        )
        self.assertEqual(entitlement.status_code, 200)
        self.assertEqual(entitlement.json()["tier"], "STANDARD")

    def test_unauthenticated_user_cannot_read_entitlement(self):
        response = self.client.get("/auth/subscription/entitlement")
        self.assertEqual(response.status_code, 401)

    def test_different_participants_have_independent_standard_allowances(self):
        first = self._create_account("ISOLATED-FIRST")
        self.assertEqual(first.status_code, 200, first.text)

        second_headers = self._register_and_login(
            "another-participant@taimobot.local", "ParticipantPassphrase-2026!"
        )
        second = self.client.post(
            "/accounts",
            headers=second_headers,
            json={"account_id": "ISOLATED-SECOND", "starting_balance": 1000,
                  "settlement_currency": "ZAR"},
        )
        self.assertEqual(second.status_code, 200, second.text)

        first_blocked = self._create_account("ISOLATED-FIRST-OVER")
        second_blocked = self.client.post(
            "/accounts",
            headers=second_headers,
            json={"account_id": "ISOLATED-SECOND-OVER", "starting_balance": 1000,
                  "settlement_currency": "ZAR"},
        )
        self.assertEqual(first_blocked.status_code, 400)
        self.assertEqual(second_blocked.status_code, 400)

    def test_upgrade_and_downgrade_preserve_existing_accounts(self):
        self._assign_tier("ADVANCED")
        account_ids = ["TIER-PRESERVE-1", "TIER-PRESERVE-2"]
        for account_id in account_ids:
            response = self._create_account(account_id)
            self.assertEqual(response.status_code, 200, response.text)

        self._assign_tier("STANDARD")
        blocked = self._create_account("TIER-PRESERVE-BLOCKED")
        self.assertEqual(blocked.status_code, 400)

        me = self.client.get("/auth/me", headers=self.participant_headers).json()
        with database.get_connection() as connection:
            rows = connection.execute(
                "SELECT account_id, starting_balance, balance, settlement_currency "
                "FROM paper_accounts WHERE owner_user_id = ? ORDER BY account_id",
                (me["user_id"],),
            ).fetchall()
        self.assertEqual([row["account_id"] for row in rows], sorted(account_ids))
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row["settlement_currency"] == "ZAR" for row in rows))

        self._assign_tier("ADVANCED")
        restored = self._create_account("TIER-PRESERVE-3")
        self.assertEqual(restored.status_code, 200, restored.text)

    def test_admin_tier_changes_write_immutable_audit_records(self):
        me = self.client.get("/auth/me", headers=self.participant_headers).json()
        self._assign_tier("ADVANCED")
        with database.get_connection() as connection:
            rows = connection.execute(
                "SELECT actor_user_id, participant_user_id, previous_tier, new_tier, reason "
                "FROM subscription_entitlement_audit WHERE participant_user_id = ?",
                (me["user_id"],),
            ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["participant_user_id"], me["user_id"])
        self.assertEqual(rows[0]["previous_tier"], "STANDARD")
        self.assertEqual(rows[0]["new_tier"], "ADVANCED")
        self.assertTrue(rows[0]["actor_user_id"])
        self.assertTrue(rows[0]["reason"])

        with self.assertRaises(sqlite3.IntegrityError):
            with database.get_connection() as connection:
                connection.execute(
                    "UPDATE subscription_entitlement_audit SET reason = 'tampered'"
                )
        with self.assertRaises(sqlite3.IntegrityError):
            with database.get_connection() as connection:
                connection.execute("DELETE FROM subscription_entitlement_audit")


if __name__ == "__main__":
    unittest.main()
