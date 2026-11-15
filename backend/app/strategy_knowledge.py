"""Persistent strategy knowledge repository.

Reference techniques are educational starting points, not validated trading signals.
All user-contributed techniques remain unvalidated until separate research gates pass.
"""

import uuid

from .database import get_connection


def list_strategies(user_id):
    with get_connection() as connection:
        rows = connection.execute(
            """
            SELECT strategy_id, owner_user_id, name, category, description,
                   entry_logic, exit_logic, risk_notes, provenance,
                   evidence_status, created_at, updated_at
            FROM strategy_knowledge
            WHERE owner_user_id IS NULL OR owner_user_id = ?
            ORDER BY
                CASE WHEN owner_user_id IS NULL THEN 0 ELSE 1 END,
                category, name
            """,
            (user_id,)
        ).fetchall()
    return {
        "count": len(rows),
        "strategies": [dict(row) for row in rows]
    }


def create_strategy(user_id, payload):
    strategy_id = "strat_" + uuid.uuid4().hex
    with get_connection() as connection:
        connection.execute(
            """
            INSERT INTO strategy_knowledge (
                strategy_id, owner_user_id, name, category, description,
                entry_logic, exit_logic, risk_notes, provenance,
                evidence_status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'USER_SUBMITTED', 'UNVALIDATED')
            """,
            (
                strategy_id, user_id, payload["name"], payload["category"],
                payload["description"], payload["entry_logic"],
                payload["exit_logic"], payload["risk_notes"]
            )
        )
        row = connection.execute(
            """
            SELECT strategy_id, owner_user_id, name, category, description,
                   entry_logic, exit_logic, risk_notes, provenance,
                   evidence_status, created_at, updated_at
            FROM strategy_knowledge
            WHERE strategy_id = ?
            """,
            (strategy_id,)
        ).fetchone()
    return dict(row)


def get_strategy(strategy_id, user_id):
    with get_connection() as connection:
        row = connection.execute(
            """
            SELECT strategy_id, owner_user_id, name, category, description,
                   entry_logic, exit_logic, risk_notes, provenance,
                   evidence_status, created_at, updated_at
            FROM strategy_knowledge
            WHERE strategy_id = ?
              AND (owner_user_id IS NULL OR owner_user_id = ?)
            """,
            (strategy_id, user_id)
        ).fetchone()
    return dict(row) if row is not None else None
