"""Daily maintenance helpers for TaimoBot's paper-trading database.

Backups are created with SQLite's online backup API. Recovery verification
restores a backup into a separate staging database and checks its integrity;
it never overwrites the live database automatically.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
import time
import tempfile
import fcntl
from contextlib import closing
from datetime import datetime
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

from . import database

SAST = ZoneInfo("Africa/Johannesburg")
BACKUP_DIR = Path(__file__).resolve().parents[1] / "backups"
_RESTORE_GUARD_FDS: dict[str, int] = {}


_RESTORE_JOURNAL_EVENTS = {
    "RESTORE_EXECUTION_STARTED",
    "RESTORE_SOURCE_PUBLISHED",
    "RESTORE_POSTCHECK_PASSED",
    "RESTORE_ROLLBACK_STARTED",
    "RESTORE_ROLLBACK_VERIFIED",
    "RESTORE_EXECUTION_COMPLETE",
    "RESTORE_EXECUTION_ROLLED_BACK",
    "RESTORE_EXECUTION_RECOVERY_REQUIRED",
}
_RESTORE_JOURNAL_MAX_BYTES = 16 * 1024 * 1024


def _restore_journal_digest(record: dict) -> str:
    payload = {key: value for key, value in record.items() if key != "record_sha256"}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _restore_journal_object_without_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key: {key}")
        result[key] = value
    return result

def _reject_restore_journal_json_constant(value):
    raise ValueError(f"non-standard JSON constant: {value}")


def verify_restore_execution_journal(
    journal_path: Path | None = None, *, include_records: bool = False,
    lock_file: bool = True, expected_identity: tuple[int, int] | None = None,
) -> dict:
    """Verify the append-only hash chain without changing the journal."""
    path = Path(journal_path or (Path(database.DB_PATH).parent / "restore-execution.audit.jsonl"))
    try:
        descriptor = os.open(str(path), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        result = {"status": "RESTORE_JOURNAL_EMPTY", "record_count": 0, "last_record_sha256": None}
        if include_records:
            result["records"] = []
        return result
    try:
        if lock_file:
            fcntl.flock(descriptor, fcntl.LOCK_SH)
        metadata = os.fstat(descriptor)
        if not __import__("stat").S_ISREG(metadata.st_mode):
            return {"status": "RESTORE_JOURNAL_UNSAFE", "record_count": 0}
        identity = (metadata.st_dev, metadata.st_ino)
        if expected_identity is not None and identity != expected_identity:
            return {"status": "RESTORE_JOURNAL_PATH_CHANGED", "record_count": 0}
        path_metadata = os.stat(path, follow_symlinks=False)
        if (
            not __import__("stat").S_ISREG(path_metadata.st_mode)
            or (path_metadata.st_dev, path_metadata.st_ino) != identity
        ):
            return {"status": "RESTORE_JOURNAL_PATH_CHANGED", "record_count": 0}
        size = metadata.st_size
        if size > _RESTORE_JOURNAL_MAX_BYTES:
            return {"status": "RESTORE_JOURNAL_INVALID", "reason": "journal exceeds size limit", "record_count": 0}
        chunks = []
        remaining = size
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                return {"status": "RESTORE_JOURNAL_INVALID", "reason": "journal truncated while reading", "record_count": 0}
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
    except OSError as error:
        return {"status": "RESTORE_JOURNAL_UNREADABLE", "reason": str(error)[:200], "record_count": 0}
    finally:
        os.close(descriptor)

    if not raw:
        result = {"status": "RESTORE_JOURNAL_EMPTY", "record_count": 0, "last_record_sha256": None}
        if include_records:
            result["records"] = []
        return result
    if not raw.endswith(b"\n"):
        return {"status": "RESTORE_JOURNAL_INVALID", "reason": "incomplete final record", "record_count": 0}
    previous = "0" * 64
    records = []
    try:
        for line in raw.splitlines():
            record = json.loads(
                line.decode("utf-8"),
                object_pairs_hook=_restore_journal_object_without_duplicate_keys,
                parse_constant=_reject_restore_journal_json_constant,
            )
            if (
                not isinstance(record, dict)
                or isinstance(record.get("sequence"), bool)
                or not isinstance(record.get("sequence"), int)
                or record.get("sequence") != len(records) + 1
            ):
                raise ValueError("invalid journal sequence")
            if (
                not isinstance(record.get("plan_id"), str)
                or not re.fullmatch(r"[a-f0-9]{32}", record["plan_id"])
                or not isinstance(record.get("execution_token"), str)
                or not re.fullmatch(r"[a-f0-9]{32}", record["execution_token"])
                or not isinstance(record.get("operator_id"), str)
                or not record["operator_id"].strip()
                or len(record["operator_id"]) > 200
                or any(ord(char) < 32 for char in record["operator_id"])
                or not isinstance(record.get("incident_ref"), str)
                or not record["incident_ref"].strip()
                or len(record["incident_ref"]) > 200
                or any(ord(char) < 32 for char in record["incident_ref"])
                or not isinstance(record.get("timestamp"), str)
                or not isinstance(record.get("details"), dict)
            ):
                raise ValueError("invalid restore journal record schema")
            try:
                parsed_timestamp = datetime.fromisoformat(record["timestamp"])
            except ValueError as error:
                raise ValueError("invalid restore journal timestamp") from error
            if parsed_timestamp.tzinfo is None or parsed_timestamp.utcoffset() is None:
                raise ValueError("restore journal timestamp must include a timezone offset")
            if record.get("previous_record_sha256") != previous:
                raise ValueError("journal hash-chain link mismatch")
            digest = _restore_journal_digest(record)
            if record.get("record_sha256") != digest:
                raise ValueError("journal record fingerprint mismatch")
            if record.get("event") not in _RESTORE_JOURNAL_EVENTS:
                raise ValueError("unknown restore journal event")
            previous = digest
            records.append(record)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError, RecursionError) as error:
        return {"status": "RESTORE_JOURNAL_INVALID", "reason": str(error)[:200], "record_count": len(records)}
    result = {
        "status": "RESTORE_JOURNAL_VERIFIED",
        "record_count": len(records),
        "last_record_sha256": previous,
    }
    if include_records:
        result["records"] = records
    return result


def _restore_event_evidence_issues(record: dict) -> list[str]:
    """Return missing/invalid event-specific evidence without inferring success."""
    details = record.get("details", {})
    if not isinstance(details, dict) or not details:
        return ["event evidence is absent"]
    event = record.get("event")
    artifacts = details.get("artifacts", {})
    requirements = {
        "RESTORE_EXECUTION_STARTED": ("live_database_sha256", "source_backup_sha256", "staged_candidate_sha256", "rollback_backup_sha256"),
        "RESTORE_SOURCE_PUBLISHED": ("staged_candidate_sha256", "published_database_sha256"),
        "RESTORE_ROLLBACK_STARTED": ("rollback_backup_sha256",),
    }
    issues = []
    if event in requirements:
        if not isinstance(artifacts, dict):
            return ["artifact fingerprints are absent"]
        for key in requirements[event]:
            value = artifacts.get(key)
            if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value):
                issues.append(f"missing or invalid artifacts.{key}")
    elif event == "RESTORE_POSTCHECK_PASSED":
        if details.get("integrity_check") != "ok":
            issues.append("integrity_check must be 'ok'")
        if details.get("foreign_key_violations") != 0 or isinstance(details.get("foreign_key_violations"), bool):
            issues.append("foreign_key_violations must be integer zero")
        if details.get("schema_check") != "passed":
            issues.append("schema_check must be 'passed'")
        value = details.get("installed_database_sha256")
        if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value):
            issues.append("missing or invalid installed_database_sha256")
    elif event == "RESTORE_ROLLBACK_VERIFIED":
        if details.get("integrity_check") != "ok":
            issues.append("integrity_check must be 'ok'")
        if details.get("foreign_key_violations") != 0 or isinstance(details.get("foreign_key_violations"), bool):
            issues.append("foreign_key_violations must be integer zero")
        value = details.get("restored_database_sha256")
        if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value):
            issues.append("missing or invalid restored_database_sha256")
    elif event == "RESTORE_EXECUTION_COMPLETE":
        value = details.get("final_database_sha256")
        if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value):
            issues.append("missing or invalid final_database_sha256")
    elif event == "RESTORE_EXECUTION_ROLLED_BACK":
        value = details.get("restored_database_sha256")
        if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value):
            issues.append("missing or invalid restored_database_sha256")
    elif event == "RESTORE_EXECUTION_RECOVERY_REQUIRED":
        reason = details.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            issues.append("recovery-required event needs a non-empty reason")
    return issues


def inspect_restore_execution_state(journal_path: Path | None = None) -> dict:
    """Read-only crash-state classification; never resumes or mutates a restore."""
    verification = verify_restore_execution_journal(journal_path, include_records=True)
    if verification["status"] not in ("RESTORE_JOURNAL_EMPTY", "RESTORE_JOURNAL_VERIFIED"):
        return {
            "status": "RESTORE_EXECUTION_INSPECTION_BLOCKED",
            "journal_status": verification["status"],
            "reason": verification.get("reason", "Journal cannot be safely inspected."),
            "executions": [],
            "automatic_action": "NONE",
            "live_database_replaced": False,
        }
    records = verification.get("records", [])
    if not records:
        return {
            "status": "NO_RECORDED_RESTORE_EXECUTION",
            "journal_status": verification["status"],
            "record_count": 0,
            "executions": [],
            "automatic_action": "NONE",
            "live_database_replaced": False,
        }

    grouped = {}
    for record in records:
        key = (record["plan_id"], record["execution_token"])
        grouped.setdefault(key, []).append(record)

    executions = []
    evidence_review = []
    terminal_events = {
        "RESTORE_EXECUTION_COMPLETE",
        "RESTORE_EXECUTION_ROLLED_BACK",
        "RESTORE_EXECUTION_RECOVERY_REQUIRED",
    }
    allowed_after = {
        "RESTORE_EXECUTION_STARTED": {
            "RESTORE_SOURCE_PUBLISHED", "RESTORE_ROLLBACK_STARTED",
            "RESTORE_EXECUTION_RECOVERY_REQUIRED",
        },
        "RESTORE_SOURCE_PUBLISHED": {
            "RESTORE_POSTCHECK_PASSED", "RESTORE_ROLLBACK_STARTED",
            "RESTORE_EXECUTION_RECOVERY_REQUIRED",
        },
        "RESTORE_POSTCHECK_PASSED": {
            "RESTORE_EXECUTION_COMPLETE", "RESTORE_ROLLBACK_STARTED",
            "RESTORE_EXECUTION_RECOVERY_REQUIRED",
        },
        "RESTORE_ROLLBACK_STARTED": {
            "RESTORE_ROLLBACK_VERIFIED", "RESTORE_EXECUTION_RECOVERY_REQUIRED",
        },
        "RESTORE_ROLLBACK_VERIFIED": {
            "RESTORE_EXECUTION_ROLLED_BACK", "RESTORE_EXECUTION_RECOVERY_REQUIRED",
        },
    }
    semantic_errors = []
    token_plan_owners = {}
    plan_execution_tokens = {}
    for (plan_id, execution_token), _items in grouped.items():
        previous_plan = token_plan_owners.get(execution_token)
        if previous_plan is not None and previous_plan != plan_id:
            semantic_errors.append({
                "plan_id": plan_id,
                "reason": "execution token reused across different restore plans",
            })
        else:
            token_plan_owners[execution_token] = plan_id

        previous_token = plan_execution_tokens.get(plan_id)
        if previous_token is not None and previous_token != execution_token:
            semantic_errors.append({
                "plan_id": plan_id,
                "reason": "restore plan reused with multiple execution tokens",
            })
        else:
            plan_execution_tokens[plan_id] = execution_token

    for (plan_id, _execution_token), items in grouped.items():
        events = [item["event"] for item in items]
        if events[0] != "RESTORE_EXECUTION_STARTED":
            semantic_errors.append({"plan_id": plan_id, "reason": "execution does not begin with STARTED"})
            continue

        identity = (items[0].get("operator_id"), items[0].get("incident_ref"))
        previous_timestamp = None
        for index, item in enumerate(items):
            if (item.get("operator_id"), item.get("incident_ref")) != identity:
                semantic_errors.append({
                    "plan_id": plan_id,
                    "reason": "operator or incident identity changed within one execution",
                })
                break
            try:
                event_timestamp = datetime.fromisoformat(item["timestamp"])
                if event_timestamp.tzinfo is None or event_timestamp.utcoffset() is None:
                    raise ValueError("timestamp must include a timezone offset")
                if previous_timestamp is not None and event_timestamp < previous_timestamp:
                    raise ValueError("event timestamp moved backwards")
                previous_timestamp = event_timestamp
            except (TypeError, ValueError, OverflowError):
                semantic_errors.append({
                    "plan_id": plan_id,
                    "reason": "invalid or non-monotonic execution timestamp",
                })
                break
            if index:
                previous_event = events[index - 1]
                event = item["event"]
                if previous_event in terminal_events or event not in allowed_after.get(previous_event, set()):
                    semantic_errors.append({
                        "plan_id": plan_id,
                        "reason": f"invalid event transition {previous_event} -> {event}",
                    })
                    break

        event_evidence = []
        for item in items:
            issues = _restore_event_evidence_issues(item)
            if issues:
                event_evidence.append({"event": item["event"], "sequence": item["sequence"], "issues": issues})
        if event_evidence:
            evidence_review.append({"plan_id": plan_id, "events": event_evidence})

        last = items[-1]
        last_event = last["event"]
        if last_event == "RESTORE_EXECUTION_COMPLETE":
            classification = "COMPLETED_REQUIRES_DATABASE_RECONCILIATION"
        elif last_event == "RESTORE_EXECUTION_ROLLED_BACK":
            classification = "ROLLED_BACK_REQUIRES_DATABASE_RECONCILIATION"
        elif last_event == "RESTORE_EXECUTION_RECOVERY_REQUIRED":
            classification = "OPERATOR_RECOVERY_REQUIRED"
        elif last_event == "RESTORE_ROLLBACK_STARTED":
            classification = "ROLLBACK_INCOMPLETE_REQUIRES_OPERATOR_REVIEW"
        elif last_event in ("RESTORE_SOURCE_PUBLISHED", "RESTORE_POSTCHECK_PASSED"):
            classification = "PUBLISHED_STATE_UNCONFIRMED_REQUIRES_OPERATOR_REVIEW"
        else:
            classification = "INTERRUPTED_BEFORE_PUBLISH_REQUIRES_OPERATOR_REVIEW"
        executions.append({
            "plan_id": plan_id,
            "operator_id": last.get("operator_id"),
            "incident_ref": last.get("incident_ref"),
            "started_at": items[0].get("timestamp"),
            "last_event_at": last.get("timestamp"),
            "last_event": last_event,
            "event_count": len(items),
            "classification": classification,
            "terminal": last_event in terminal_events,
            "evidence_status": "INCOMPLETE_REQUIRES_OPERATOR_REVIEW" if event_evidence else "EVENT_EVIDENCE_PRESENT",
            "events_missing_evidence": len(event_evidence),
        })

    if semantic_errors:
        return {
            "status": "RESTORE_JOURNAL_SEMANTIC_INVALID",
            "journal_status": verification["status"],
            "record_count": len(records),
            "executions": executions,
            "semantic_errors": semantic_errors,
            "automatic_action": "NONE",
            "live_database_replaced": False,
        }
    needs_review = any(not item["terminal"] or item["classification"] in (
        "OPERATOR_RECOVERY_REQUIRED",
        "COMPLETED_REQUIRES_DATABASE_RECONCILIATION",
        "ROLLED_BACK_REQUIRES_DATABASE_RECONCILIATION",
    ) for item in executions)
    return {
        "status": "RESTORE_EXECUTION_REVIEW_REQUIRED" if needs_review else "RESTORE_EXECUTION_TERMINAL_EVENTS_RECORDED",
        "journal_status": verification["status"],
        "record_count": len(records),
        "executions": executions,
        "evidence_review": evidence_review,
        "automatic_action": "NONE",
        "live_database_replaced": False,
    }



def reconcile_restore_execution_artifacts(
    plan_id: str,
    execution_token: str,
    artifact_paths: dict[str, Path],
    *,
    journal_path: Path | None = None,
) -> dict:
    """Read-only comparison of recorded artifact hashes with current files."""
    if not isinstance(plan_id, str) or not re.fullmatch(r"[a-f0-9]{32}", plan_id):
        raise ValueError("A valid restore plan ID is required.")
    if not isinstance(execution_token, str) or not re.fullmatch(r"[a-f0-9]{32}", execution_token):
        raise ValueError("A valid execution token is required.")
    allowed = {"live_database", "source_backup", "staged_candidate", "rollback_backup"}
    if not isinstance(artifact_paths, dict) or set(artifact_paths) != allowed:
        raise ValueError("Exactly four named restore artifact paths are required.")

    verification = verify_restore_execution_journal(journal_path, include_records=True)
    if verification["status"] not in ("RESTORE_JOURNAL_EMPTY", "RESTORE_JOURNAL_VERIFIED"):
        return {
            "status": "RESTORE_EVIDENCE_RECONCILIATION_BLOCKED",
            "journal_status": verification["status"],
            "artifacts": [],
            "database_state_verified": False,
            "automatic_action": "NONE",
            "live_database_replaced": False,
        }
    execution_state = inspect_restore_execution_state(journal_path)
    if execution_state.get("status") == "RESTORE_JOURNAL_SEMANTIC_INVALID":
        return {
            "status": "RESTORE_EVIDENCE_RECONCILIATION_BLOCKED",
            "journal_status": execution_state["status"],
            "semantic_errors": execution_state.get("semantic_errors", []),
            "artifacts": [],
            "database_state_verified": False,
            "automatic_action": "NONE",
            "live_database_replaced": False,
        }
    matching_records = [
        record for record in verification.get("records", [])
        if record.get("plan_id") == plan_id and record.get("execution_token") == execution_token
    ]
    if not matching_records:
        return {
            "status": "RESTORE_EVIDENCE_NOT_FOUND",
            "journal_status": verification["status"],
            "artifacts": [],
            "database_state_verified": False,
            "automatic_action": "NONE",
            "live_database_replaced": False,
        }

    expected = {}
    conflicting = set()
    for record in matching_records:
        artifacts = record.get("details", {}).get("artifacts", {})
        if isinstance(artifacts, dict):
            for key in allowed:
                value = artifacts.get(f"{key}_sha256")
                if isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value):
                    if key in expected and expected[key] != value:
                        conflicting.add(key)
                    expected[key] = value

    artifact_sidecars = {}
    for artifact_key in allowed:
        artifact_path = Path(artifact_paths[artifact_key])
        try:
            artifact_sidecars[artifact_key] = inspect_sqlite_sidecars(artifact_path)
        except (OSError, ValueError):
            artifact_sidecars[artifact_key] = {
                "status": "SIDECARS_UNSAFE_OR_UNREADABLE",
                "sidecars": [],
                "mutation_performed": False,
            }
    live_sidecars = artifact_sidecars["live_database"]

    results = []
    artifact_identities = {}
    all_verified = not conflicting and all(
        inventory["status"] == "SIDECARS_CLEAR"
        for inventory in artifact_sidecars.values()
    )
    for key in ("live_database", "source_backup", "staged_candidate", "rollback_backup"):
        path = Path(artifact_paths[key])
        item = {
            "artifact": key,
            "file_name": path.name,
            "expected_sha256": expected.get(key),
            "current_sha256": None,
            "fingerprint_status": "CONFLICTING_JOURNAL_EVIDENCE" if key in conflicting else (
                "NOT_RECORDED" if key not in expected else "UNVERIFIED"
            ),
            "integrity_check": "NOT_CHECKED",
            "foreign_key_violations": None,
        }
        try:
            descriptor = os.open(str(path), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            try:
                metadata = os.fstat(descriptor)
                if not __import__("stat").S_ISREG(metadata.st_mode):
                    raise ValueError("artifact is not a regular file")
                identity = (metadata.st_dev, metadata.st_ino)
                if identity in artifact_identities:
                    raise ValueError(
                        f"artifact aliases {artifact_identities[identity]} through the same filesystem object"
                    )
                artifact_identities[identity] = key
                def hash_open_descriptor() -> str:
                    digest = hashlib.sha256()
                    offset = 0
                    while True:
                        chunk = os.pread(descriptor, 1024 * 1024, offset)
                        if not chunk:
                            break
                        digest.update(chunk)
                        offset += len(chunk)
                    return digest.hexdigest()

                current_hash = hash_open_descriptor()
                item["current_sha256"] = current_hash
                if key not in conflicting and key in expected:
                    item["fingerprint_status"] = "MATCH" if current_hash == expected[key] else "MISMATCH"

                # Never open any SQLite artifact through SQLite while adjacent
                # WAL/SHM sidecars exist; the main file alone may be stale.
                if artifact_sidecars[key]["status"] != "SIDECARS_CLEAR":
                    item["integrity_check"] = "SKIPPED_SIDECARS_PRESENT"
                    item["sidecars"] = artifact_sidecars[key]
                    all_verified = False
                else:
                    # SQLite opens by pathname, not our already-open descriptor.
                    # Check pathname identity before and after its read-only checks,
                    # then re-hash the original descriptor to detect replacement or
                    # in-place mutation during the inspection window.
                    before_path = os.stat(path, follow_symlinks=False)
                    if (before_path.st_dev, before_path.st_ino) != identity or not __import__("stat").S_ISREG(before_path.st_mode):
                        raise ValueError("artifact path changed after fingerprinting")
                    uri = path.absolute().as_uri() + "?mode=ro"
                    with closing(sqlite3.connect(uri, uri=True, timeout=10)) as connection:
                        integrity = connection.execute("PRAGMA integrity_check").fetchone()
                        violations = connection.execute("PRAGMA foreign_key_check").fetchall()
                    sidecars_after = inspect_sqlite_sidecars(path)
                    if sidecars_after != artifact_sidecars[key]:
                        raise ValueError("artifact sidecars changed during integrity inspection")
                    after_path = os.stat(path, follow_symlinks=False)
                    after_fd = os.fstat(descriptor)
                    if (
                        (after_path.st_dev, after_path.st_ino) != identity
                        or (after_fd.st_dev, after_fd.st_ino) != identity
                        or not __import__("stat").S_ISREG(after_path.st_mode)
                        or hash_open_descriptor() != current_hash
                    ):
                        raise ValueError("artifact changed during read-only integrity inspection")
                    item["integrity_check"] = integrity[0] if integrity else "NO_RESULT"
                    item["foreign_key_violations"] = len(violations)
                    if item["integrity_check"] != "ok" or violations:
                        all_verified = False
            finally:
                os.close(descriptor)
            if item["fingerprint_status"] in (
                "MISMATCH", "UNVERIFIED", "NOT_RECORDED", "CONFLICTING_JOURNAL_EVIDENCE"
            ):
                all_verified = False
        except (OSError, ValueError, sqlite3.Error) as error:
            item["fingerprint_status"] = "UNSAFE_OR_MISSING"
            item["error"] = str(error)[:160]
            all_verified = False
        results.append(item)

    # Compare fingerprints that describe the same lifecycle state. The
    # published database hash is intentionally distinct from the staged file
    # hash; only postcheck/terminal hashes may describe the installed live DB.
    lifecycle_issues = []
    start_events = [record for record in matching_records if record.get("event") == "RESTORE_EXECUTION_STARTED"]
    publish_events = [record for record in matching_records if record.get("event") == "RESTORE_SOURCE_PUBLISHED"]
    rollback_start_events = [record for record in matching_records if record.get("event") == "RESTORE_ROLLBACK_STARTED"]
    rollback_verified_events = [record for record in matching_records if record.get("event") == "RESTORE_ROLLBACK_VERIFIED"]
    initial_artifacts = start_events[0].get("details", {}).get("artifacts", {}) if start_events else {}
    if publish_events and start_events:
        published_staged = publish_events[-1].get("details", {}).get("artifacts", {}).get("staged_candidate_sha256")
        initial_staged = initial_artifacts.get("staged_candidate_sha256")
        if published_staged != initial_staged:
            lifecycle_issues.append("source-published staged-candidate fingerprint disagrees with execution-start evidence")
    postchecks = [
        record.get("details", {}).get("installed_database_sha256")
        for record in matching_records
        if record.get("event") == "RESTORE_POSTCHECK_PASSED"
    ]
    completions = [
        record.get("details", {}).get("final_database_sha256")
        for record in matching_records
        if record.get("event") == "RESTORE_EXECUTION_COMPLETE"
    ]
    rollback_checks = [
        record.get("details", {}).get("restored_database_sha256")
        for record in matching_records
        if record.get("event") == "RESTORE_ROLLBACK_VERIFIED"
    ]
    rollback_completions = [
        record.get("details", {}).get("restored_database_sha256")
        for record in matching_records
        if record.get("event") == "RESTORE_EXECUTION_ROLLED_BACK"
    ]
    if postchecks and publish_events:
        published_hash = publish_events[-1].get("details", {}).get("artifacts", {}).get("published_database_sha256")
        if postchecks[-1] != published_hash:
            lifecycle_issues.append("postcheck installed-database fingerprint disagrees with source-published evidence")
    if postchecks and completions and postchecks[-1] != completions[-1]:
        lifecycle_issues.append("postcheck and completion fingerprints describe different installed database bytes")
    if rollback_start_events and start_events:
        initial_rollback = initial_artifacts.get("rollback_backup_sha256")
        rollback_start_hash = rollback_start_events[-1].get("details", {}).get("artifacts", {}).get("rollback_backup_sha256")
        if rollback_start_hash != initial_rollback:
            lifecycle_issues.append("rollback-start backup fingerprint disagrees with execution-start evidence")
    if rollback_checks and rollback_start_events:
        rollback_start_hash = rollback_start_events[-1].get("details", {}).get("artifacts", {}).get("rollback_backup_sha256")
        if rollback_checks[-1] != rollback_start_hash:
            lifecycle_issues.append("rollback verification fingerprint disagrees with rollback-backup evidence")
    if rollback_checks and rollback_completions and rollback_checks[-1] != rollback_completions[-1]:
        lifecycle_issues.append("rollback verification and rollback completion fingerprints disagree")

    terminal_hash = None
    terminal_event = None
    for record in reversed(matching_records):
        event = record.get("event")
        details = record.get("details", {})
        if event == "RESTORE_EXECUTION_COMPLETE":
            terminal_hash, terminal_event = details.get("final_database_sha256"), event
            break
        if event == "RESTORE_EXECUTION_ROLLED_BACK":
            terminal_hash, terminal_event = details.get("restored_database_sha256"), event
            break
    live_result = next((item for item in results if item["artifact"] == "live_database"), None)
    if terminal_hash and live_result and live_sidecars["status"] == "SIDECARS_CLEAR":
        if live_result.get("current_sha256") != terminal_hash:
            lifecycle_issues.append(
                f"current live database fingerprint does not match terminal {terminal_event} evidence"
            )
    event_evidence_review = []
    for record in matching_records:
        missing_evidence = _restore_event_evidence_issues(record)
        if missing_evidence:
            event_evidence_review.append({
                "sequence": record.get("sequence"),
                "event": record.get("event"),
                "issues": missing_evidence,
            })
    # Re-read the append-only journal after artifact inspection. A valid
    # initial snapshot is insufficient if another writer changed the journal
    # while the files were being fingerprinted.
    final_verification = verify_restore_execution_journal(journal_path, include_records=True)
    if (
        final_verification.get("status") not in ("RESTORE_JOURNAL_EMPTY", "RESTORE_JOURNAL_VERIFIED")
        or final_verification.get("records") != verification.get("records")
    ):
        lifecycle_issues.append("restore execution journal changed during artifact reconciliation")
    if lifecycle_issues or event_evidence_review:
        all_verified = False

    return {
        "status": "RESTORE_EVIDENCE_MATCHED" if all_verified else "RESTORE_EVIDENCE_REVIEW_REQUIRED",
        "journal_status": verification["status"],
        "plan_id": plan_id,
        "execution_token": execution_token,
        "event_count": len(matching_records),
        "conflicting_fingerprints": sorted(conflicting),
        "lifecycle_consistency_issues": lifecycle_issues,
        "event_evidence_review": event_evidence_review,
        "terminal_event": terminal_event,
        "artifacts": results,
        "live_database_sidecars": live_sidecars,
        "artifact_sidecars": artifact_sidecars,
        "evidence_scope": "FILE_FINGERPRINTS_AND_SQLITE_INTEGRITY_ONLY",
        "recoverability_verified": False,
        "database_state_verified": False,
        "automatic_action": "NONE",
        "live_database_replaced": False,
    }


def append_restore_execution_event(
    event: str,
    plan_id: str,
    execution_token: str,
    operator_id: str,
    incident_ref: str,
    *,
    details: dict | None = None,
    journal_path: Path | None = None,
) -> dict:
    """Append one fsynced, hash-chained restore event to an independent journal.

    This journal deliberately lives outside SQLite so it remains writable while
    the application database is quiesced. It records events only; it does not
    perform or authorize a restore.
    """
    if event not in _RESTORE_JOURNAL_EVENTS:
        raise ValueError("Unsupported restore execution event.")
    for value, label in ((plan_id, "plan_id"), (execution_token, "execution_token")):
        if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{32}", value):
            raise ValueError(f"A valid {label} is required.")
    for value, label in ((operator_id, "operator_id"), (incident_ref, "incident_ref")):
        if not isinstance(value, str) or not value.strip() or len(value) > 200 or any(ord(char) < 32 for char in value):
            raise ValueError(f"A valid {label} is required.")
    if details is not None and not isinstance(details, dict):
        raise ValueError("Journal details must be an object.")
    details = details or {}
    try:
        details_encoded = json.dumps(details, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise ValueError("Journal details must be JSON serializable.") from error
    if len(details_encoded) > 8192:
        raise ValueError("Journal details exceed the 8 KiB limit.")

    path = Path(journal_path or (Path(database.DB_PATH).parent / "restore-execution.audit.jsonl"))
    path.parent.mkdir(parents=True, exist_ok=True)
    existed = path.exists()
    descriptor = os.open(
        str(path),
        os.O_CREAT | os.O_RDWR | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        metadata = os.fstat(descriptor)
        if not __import__("stat").S_ISREG(metadata.st_mode):
            raise RuntimeError("Restore execution journal is not a regular file.")
        identity = (metadata.st_dev, metadata.st_ino)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        path_metadata = os.stat(path, follow_symlinks=False)
        if (
            not __import__("stat").S_ISREG(path_metadata.st_mode)
            or (path_metadata.st_dev, path_metadata.st_ino) != identity
        ):
            raise RuntimeError("Restore execution journal path changed before append; append refused.")
        verification = verify_restore_execution_journal(
            path, lock_file=False, expected_identity=identity
        )
        current_metadata = os.fstat(descriptor)
        current_path_metadata = os.stat(path, follow_symlinks=False)
        if (
            (current_metadata.st_dev, current_metadata.st_ino) != identity
            or (current_path_metadata.st_dev, current_path_metadata.st_ino) != identity
            or current_metadata.st_size != current_path_metadata.st_size
            or current_metadata.st_size != metadata.st_size
        ):
            raise RuntimeError("Restore execution journal changed during verification; append refused.")
        if verification["status"] not in ("RESTORE_JOURNAL_EMPTY", "RESTORE_JOURNAL_VERIFIED"):
            raise RuntimeError("Restore execution journal failed integrity verification; append refused.")
        record = {
            "sequence": verification["record_count"] + 1,
            "timestamp": datetime.now(SAST).isoformat(),
            "event": event,
            "plan_id": plan_id,
            "execution_token": execution_token,
            "operator_id": operator_id.strip(),
            "incident_ref": incident_ref.strip(),
            "details": details,
            "previous_record_sha256": verification["last_record_sha256"] or ("0" * 64),
        }
        record["record_sha256"] = _restore_journal_digest(record)
        encoded = (json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")
        current_size = os.fstat(descriptor).st_size
        if current_size + len(encoded) > _RESTORE_JOURNAL_MAX_BYTES:
            raise ValueError("Restore execution journal would exceed the size limit.")
        written_total = 0
        while written_total < len(encoded):
            written = os.write(descriptor, encoded[written_total:])
            if written <= 0:
                raise OSError("Could not complete restore journal append.")
            written_total += written
        os.fsync(descriptor)
        if not existed:
            directory_fd = os.open(str(path.parent), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        return {
            "status": "RESTORE_JOURNAL_EVENT_RECORDED",
            "event": event,
            "sequence": record["sequence"],
            "record_sha256": record["record_sha256"],
            "journal_file": path.name,
        }
    finally:
        os.close(descriptor)


def is_maintenance_window(now: datetime | None = None) -> bool:
    """Return True during the configured SAST maintenance window [23:00, 00:00)."""
    local_now = (now or datetime.now(SAST)).astimezone(SAST)
    return local_now.hour == 23


def create_verified_backup(
    source_path: Path | None = None,
    backup_dir: Path | None = None,
    now: datetime | None = None,
) -> dict:
    """Create an online SQLite backup and verify it before publishing the file."""
    source = Path(source_path or database.DB_PATH)
    destination_dir = Path(backup_dir or BACKUP_DIR)
    if (
        source.resolve() == Path(database.DB_PATH).resolve()
        and is_restore_lock_active()
    ):
        raise RuntimeError(
            "Live database backup blocked: a controlled restore has quiesced the application."
        )
    destination_dir.mkdir(parents=True, exist_ok=True)

    if not source.exists():
        raise FileNotFoundError(f"Live database does not exist: {source}")

    local_now = (now or datetime.now(SAST)).astimezone(SAST)
    stamp = local_now.strftime("%Y%m%d-%H%M%S")
    # Multiple workers or operator retries can start within the same second.
    # Give every backup attempt its own final and temporary path so SQLite
    # copies cannot overwrite or delete one another during publication.
    attempt_id = uuid4().hex
    final_path = destination_dir / f"taimobot-{stamp}-{attempt_id}.sqlite3"
    temporary_path = destination_dir / f".taimobot-{stamp}-{attempt_id}.tmp"

    guard_fd = None
    try:
        # Hold a shared process-wide guard for the entire live-database backup.
        # This closes the race between the initial marker check and sqlite3.connect.
        if source.resolve() == Path(database.DB_PATH).resolve():
            guard_fd = database._acquire_connection_guard_fd()
        with sqlite3.connect(str(source), timeout=30) as live:
            with sqlite3.connect(str(temporary_path), timeout=30) as backup:
                live.backup(backup)

        with sqlite3.connect(str(temporary_path), timeout=30) as check:
            integrity = check.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok":
                raise RuntimeError(f"Backup integrity check failed: {integrity}")

        temporary_path.replace(final_path)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise
    finally:
        if guard_fd is not None:
            os.close(guard_fd)

    return {
        "status": "BACKUP_VERIFIED",
        "backup_file": str(final_path),
        "integrity_check": "ok",
        "created_at": local_now.isoformat(),
        "timezone": "Africa/Johannesburg",
    }


def _protected_backup_paths() -> set[Path]:
    """Return the latest completed recovery point and files held by active attempts.

    A metadata read failure must stop retention rather than risk deleting a
    backup whose lifecycle cannot be established.
    """
    protected: set[Path] = set()
    with database.get_connection() as connection:
        tables = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        if "maintenance_runs" in tables:
            row = connection.execute(
                """
                SELECT backup_file
                FROM maintenance_runs
                WHERE status = 'COMPLETE' AND backup_file IS NOT NULL
                ORDER BY completed_at DESC, run_date DESC
                LIMIT 1
                """
            ).fetchone()
            if row and row["backup_file"]:
                protected.add(Path(row["backup_file"]).resolve())
        if "maintenance_run_attempts" in tables:
            rows = connection.execute(
                """
                SELECT backup_file
                FROM maintenance_run_attempts
                WHERE status = 'RUNNING' AND backup_file IS NOT NULL
                """
            ).fetchall()
            protected.update(
                Path(row["backup_file"]).resolve()
                for row in rows if row["backup_file"]
            )
            # A verified backup can be produced before a later maintenance
            # step fails or before its worker is superseded. Keep the newest
            # referenced recovery file for each non-success terminal outcome
            # as well as the latest successful run and all active attempts.
            # Older terminal-outcome files remain eligible after 365 days,
            # avoiding indefinite retention growth from the audit history.
            for terminal_status in ("FAILED", "SUPERSEDED"):
                row = connection.execute(
                    """
                    SELECT backup_file
                    FROM maintenance_run_attempts
                    WHERE status = ? AND backup_file IS NOT NULL
                    ORDER BY completed_at DESC, started_at DESC
                    LIMIT 1
                    """,
                    (terminal_status,),
                ).fetchone()
                if row and row["backup_file"]:
                    protected.add(Path(row["backup_file"]).resolve())
    return protected


def cleanup_expired_backups(
    backup_dir: Path | None = None,
    *,
    now: datetime | None = None,
    retention_days: int = 365,
    dry_run: bool = True,
    live_database_path: Path | None = None,
) -> dict:
    """Safely plan or remove expired managed backups after validating recovery points.

    Only regular, non-symlink TaimoBot backup files in the backup directory are
    considered. At least one verified backup inside the retention window must
    exist before any expired file can be removed. The newest verified backup
    and the live database are always protected. Dry-run is the default.
    """
    if isinstance(retention_days, bool) or not isinstance(retention_days, int) or retention_days < 365:
        raise ValueError("retention_days must be an integer of at least 365 days")

    directory = Path(backup_dir or BACKUP_DIR)
    if not directory.exists():
        return {
            "status": "NO_BACKUP_DIRECTORY",
            "dry_run": dry_run,
            "retention_days": retention_days,
            "candidates": [],
            "deleted": [],
        }
    if not directory.is_dir():
        raise NotADirectoryError(f"Backup path is not a directory: {directory}")

    local_now = (now or datetime.now(SAST)).astimezone(SAST)
    cutoff = local_now.timestamp() - retention_days * 24 * 60 * 60
    live_path = Path(live_database_path or database.DB_PATH).resolve()
    try:
        protected_paths = _protected_backup_paths()
    except (sqlite3.Error, OSError, ValueError) as error:
        return {
            "status": "BACKUP_RETENTION_BLOCKED",
            "reason": "Maintenance backup references could not be checked.",
            "metadata_error": str(error)[:300],
            "dry_run": dry_run,
            "retention_days": retention_days,
            "candidates": [],
            "deleted": [],
            "live_database_replaced": False,
        }
    verified: list[tuple[Path, float, tuple[int, int, int, int]]] = []
    invalid: list[str] = []

    # If the directory cannot be enumerated reliably, do not make any
    # deletion decisions from a partial view of the recovery set.
    try:
        directory_entries = list(directory.iterdir())
    except OSError as error:
        return {
            "status": "BACKUP_RETENTION_BLOCKED",
            "reason": "Backup directory could not be inspected safely.",
            "metadata_error": str(error)[:300],
            "dry_run": dry_run,
            "retention_days": retention_days,
            "candidates": [],
            "deleted": [],
            "live_database_replaced": False,
        }

    for candidate in directory_entries:
        if not candidate.name.startswith("taimobot-") or not candidate.name.endswith(".sqlite3"):
            continue
        try:
            if candidate.is_symlink() or not candidate.is_file():
                continue
            resolved_candidate = candidate.resolve()
            if resolved_candidate == live_path:
                continue
            with sqlite3.connect(f"file:{resolved_candidate}?mode=ro", uri=True, timeout=10) as check:
                integrity = check.execute("PRAGMA integrity_check").fetchone()
            if not integrity or integrity[0] != "ok":
                invalid.append(candidate.name)
                continue
            file_stat = candidate.stat(follow_symlinks=False)
            identity = (file_stat.st_dev, file_stat.st_ino, file_stat.st_size, file_stat.st_mtime_ns)
            verified.append((candidate, file_stat.st_mtime, identity))
        except (sqlite3.Error, OSError, ValueError):
            # An unreadable or unstable file is never considered for deletion.
            invalid.append(candidate.name)

    verified.sort(key=lambda item: (item[1], item[0].name), reverse=True)
    newest_verified = verified[0][0] if verified else None
    has_recent_recovery_point = any(timestamp >= cutoff for _, timestamp, _ in verified)

    candidates = [
        path for path, timestamp, _ in verified
        if timestamp < cutoff
        and path != newest_verified
        and path.resolve() not in protected_paths
    ]
    if not has_recent_recovery_point:
        candidates = []

    deleted: list[str] = []
    deletion_failures: list[str] = []
    if not dry_run:
        original_identities = {path: identity for path, _, identity in verified}
        for candidate in candidates:
            # Re-check identity, age and integrity immediately before deletion.
            # A replaced file must not inherit the older file's expiry decision.
            try:
                if candidate.is_symlink() or not candidate.is_file():
                    continue
                before = candidate.stat(follow_symlinks=False)
                before_identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                if (
                    before_identity != original_identities[candidate]
                    or before.st_mtime >= cutoff
                    or candidate.resolve() == live_path
                    or candidate.resolve() in protected_paths
                ):
                    continue
                with sqlite3.connect(f"file:{candidate.resolve()}?mode=ro", uri=True, timeout=10) as check:
                    integrity = check.execute("PRAGMA integrity_check").fetchone()
                if not integrity or integrity[0] != "ok":
                    invalid.append(candidate.name)
                    continue
                after = candidate.stat(follow_symlinks=False)
                after_identity = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
                if (
                    candidate.is_symlink()
                    or after_identity != original_identities[candidate]
                    or after.st_mtime >= cutoff
                ):
                    continue
                candidate.unlink()
                deleted.append(candidate.name)
            except (sqlite3.Error, OSError, ValueError):
                invalid.append(candidate.name)
                deletion_failures.append(candidate.name)

    return {
        "status": (
            "BACKUP_RETENTION_DRY_RUN"
            if dry_run
            else "BACKUP_RETENTION_BLOCKED"
            if deletion_failures
            else "BACKUP_RETENTION_COMPLETE"
        ),
        "deletion_failures": deletion_failures,
        "dry_run": dry_run,
        "retention_days": retention_days,
        "cutoff": datetime.fromtimestamp(cutoff, SAST).isoformat(),
        "verified_backup_count": len(verified),
        "recent_recovery_point_verified": has_recent_recovery_point,
        "newest_verified_backup": newest_verified.name if newest_verified else None,
        "candidates": [path.name for path in candidates],
        "deleted": deleted,
        "invalid_or_unreadable": sorted(set(invalid)),
        "live_database_replaced": False,
    }




def inspect_recovery_artifact_inventory(
    backup_dir: Path | None = None,
    *,
    journal_path: Path | None = None,
) -> dict:
    """Read-only cross-check of managed recovery files against persisted references.

    This diagnostic reports orphaned, missing, unsafe, and fingerprint-conflicting
    artifacts. It never deletes, repairs, renames, or stages recovery files.
    """
    root = Path(backup_dir or BACKUP_DIR)
    staging = root / "recovery-staging"
    journal = Path(
        journal_path
        or (Path(database.DB_PATH).parent / "restore-execution.audit.jsonl")
    )
    result_base = {
        "read_only": True,
        "automatic_deletion": False,
        "automatic_repair": False,
        "live_database_replaced": False,
    }
    if not root.exists():
        return {
            **result_base,
            "status": "NO_BACKUP_DIRECTORY",
            "artifacts": [],
            "orphaned_artifacts": [],
            "missing_references": [],
            "issues": [],
        }
    if root.is_symlink() or not root.is_dir():
        return {
            **result_base,
            "status": "RECOVERY_ARTIFACT_INVENTORY_BLOCKED",
            "artifacts": [],
            "orphaned_artifacts": [],
            "missing_references": [],
            "issues": ["BACKUP_DIRECTORY_UNSAFE"],
        }

    def directory_identity(path: Path):
        metadata = path.stat(follow_symlinks=False)
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise OSError("managed recovery directory was replaced or is unsafe")
        return (
            metadata.st_dev, metadata.st_ino, metadata.st_mode,
            metadata.st_uid, metadata.st_gid, metadata.st_nlink,
            metadata.st_mtime_ns, metadata.st_ctime_ns,
        )

    try:
        root_identity = directory_identity(root)
        staging_identity = directory_identity(staging) if staging.exists() or staging.is_symlink() else None
    except OSError as error:
        return {
            **result_base,
            "status": "RECOVERY_ARTIFACT_INVENTORY_BLOCKED",
            "artifacts": [],
            "orphaned_artifacts": [],
            "missing_references": [],
            "issues": ["BACKUP_DIRECTORY_UNSAFE", str(error)[:180]],
        }

    def assert_managed_directories_unchanged():
        if directory_identity(root) != root_identity:
            raise OSError("backup directory identity changed during inventory")
        current_staging = directory_identity(staging) if staging.exists() or staging.is_symlink() else None
        if current_staging != staging_identity:
            raise OSError("recovery staging directory identity changed during inventory")

    # A journal with an invalid hash chain or lifecycle semantics cannot be
    # used to decide that an artifact is unreferenced.
    journal_verification = verify_restore_execution_journal(
        journal, include_records=True
    )
    if journal_verification.get("status") not in (
        "RESTORE_JOURNAL_EMPTY", "RESTORE_JOURNAL_VERIFIED"
    ):
        return {
            **result_base,
            "status": "RECOVERY_ARTIFACT_INVENTORY_BLOCKED",
            "journal_status": journal_verification.get("status"),
            "artifacts": [],
            "orphaned_artifacts": [],
            "missing_references": [],
            "issues": ["RESTORE_EXECUTION_JOURNAL_UNTRUSTED"],
        }
    execution_state = inspect_restore_execution_state(journal)
    if execution_state.get("status") in (
        "RESTORE_EXECUTION_INSPECTION_BLOCKED",
        "RESTORE_JOURNAL_SEMANTIC_INVALID",
    ):
        return {
            **result_base,
            "status": "RECOVERY_ARTIFACT_INVENTORY_BLOCKED",
            "journal_status": execution_state.get("status"),
            "artifacts": [],
            "orphaned_artifacts": [],
            "missing_references": [],
            "issues": ["RESTORE_EXECUTION_LIFECYCLE_UNTRUSTED"],
        }

    references: dict[str, list[dict]] = {}
    expected_hashes: dict[tuple[str, str], str] = {}
    database_plan_ids: set[str] = set()
    issues: list[str] = []
    missing_references: list[dict] = []
    metadata_table_names = (
        "maintenance_runs",
        "maintenance_run_attempts",
        "maintenance_restore_plans",
        "maintenance_restore_artifacts",
    )

    def metadata_state_fingerprint(connection):
        present = {
            row["name"] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        state = {}
        for table in metadata_table_names:
            if table not in present:
                state[table] = None
                continue
            columns = [
                row["name"] for row in connection.execute(
                    f"PRAGMA table_info({table})"
                )
            ]
            rows = connection.execute(f"SELECT * FROM {table}").fetchall()
            serialized_rows = sorted(
                json.dumps(dict(row), sort_keys=True, default=str)
                for row in rows
            )
            state[table] = {"columns": columns, "rows": serialized_rows}
        encoded = json.dumps(state, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    database_path = Path(database.DB_PATH)

    def database_file_identity():
        metadata = os.stat(database_path, follow_symlinks=False)
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise OSError("live database path was replaced or is unsafe")
        return (metadata.st_dev, metadata.st_ino)

    try:
        initial_database_identity = database_file_identity()
        with database.get_connection() as connection:
            tables = {
                row["name"] for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }

            def add_reference(raw_path, source, plan_id=None):
                if not raw_path:
                    return
                candidate = Path(str(raw_path))
                try:
                    resolved = candidate.resolve(strict=False)
                    resolved_root = root.resolve()
                    resolved_staging = staging.resolve()
                    if resolved != resolved_root and resolved_root not in resolved.parents:
                        if resolved != resolved_staging and resolved_staging not in resolved.parents:
                            issues.append("REFERENCE_OUTSIDE_MANAGED_BACKUP_DIRECTORIES")
                            missing_references.append({
                                "source": source, "path": str(raw_path),
                                "status": "OUTSIDE_MANAGED_DIRECTORY",
                            })
                            return
                    key = str(resolved)
                    references.setdefault(key, []).append({
                        "source": source, "plan_id": plan_id,
                    })
                except (OSError, RuntimeError, ValueError):
                    issues.append("REFERENCE_PATH_UNRESOLVABLE")
                    missing_references.append({
                        "source": source, "path": str(raw_path),
                        "status": "UNRESOLVABLE",
                    })

            for table, required in (
                ("maintenance_runs", {"backup_file"}),
                ("maintenance_run_attempts", {"backup_file"}),
                ("maintenance_restore_plans", {
                    "plan_id", "backup_file", "staging_file",
                    "pre_restore_backup_file",
                }),
                ("maintenance_restore_artifacts", {
                    "plan_id", "source_sha256", "staged_sha256", "rollback_sha256",
                }),
            ):
                if table not in tables:
                    continue
                actual = {
                    row["name"] for row in connection.execute(
                        f"PRAGMA table_info({table})"
                    )
                }
                if not required.issubset(actual):
                    raise sqlite3.DatabaseError(
                        f"{table} schema is incomplete for recovery inventory"
                    )

            initial_metadata_fingerprint = metadata_state_fingerprint(connection)
            if database_file_identity() != initial_database_identity:
                raise sqlite3.DatabaseError("live database identity changed during metadata snapshot")

            for table in ("maintenance_runs", "maintenance_run_attempts"):
                if table in tables:
                    rows = connection.execute(
                        f"SELECT backup_file FROM {table} WHERE backup_file IS NOT NULL"
                    ).fetchall()
                    for row in rows:
                        add_reference(row["backup_file"], table)

            if "maintenance_restore_plans" in tables:
                plans = connection.execute(
                    """
                    SELECT plan_id, backup_file, staging_file, pre_restore_backup_file, status
                    FROM maintenance_restore_plans
                    """
                ).fetchall()
                for plan in plans:
                    database_plan_ids.add(str(plan["plan_id"]))
                    add_reference(plan["backup_file"], "maintenance_restore_plans.source", plan["plan_id"])
                    add_reference(plan["staging_file"], "maintenance_restore_plans.staged", plan["plan_id"])
                    add_reference(plan["pre_restore_backup_file"], "maintenance_restore_plans.rollback", plan["plan_id"])

            if "maintenance_restore_artifacts" in tables:
                rows = connection.execute(
                    """
                    SELECT plan_id, source_sha256, staged_sha256, rollback_sha256
                    FROM maintenance_restore_artifacts
                    """
                ).fetchall()
                for row in rows:
                    for role, field in (
                        ("source", "source_sha256"),
                        ("staged", "staged_sha256"),
                        ("rollback", "rollback_sha256"),
                    ):
                        value = row[field]
                        if isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value):
                            expected_hashes[(str(row["plan_id"]), role)] = value
                        else:
                            issues.append("RESTORE_ARTIFACT_FINGERPRINT_INVALID")
    except (sqlite3.Error, OSError, ValueError) as error:
        return {
            **result_base,
            "status": "RECOVERY_ARTIFACT_INVENTORY_BLOCKED",
            "journal_status": journal_verification.get("status"),
            "artifacts": [],
            "orphaned_artifacts": [],
            "missing_references": [],
            "issues": ["MAINTENANCE_METADATA_UNREADABLE", str(error)[:180]],
        }

    journal_plan_ids = {
        str(record.get("plan_id"))
        for record in journal_verification.get("records", [])
        if record.get("plan_id")
    }
    for plan_id in sorted(journal_plan_ids - database_plan_ids):
        issues.append("JOURNAL_PLAN_WITHOUT_DATABASE_PLAN")
        missing_references.append({
            "source": "restore_execution_journal",
            "plan_id": plan_id,
            "status": "DATABASE_PLAN_MISSING",
        })

    artifacts: list[dict] = []
    seen_inodes: dict[tuple[int, int], str] = {}
    artifact_snapshots: dict[tuple[str, str, str], tuple[int, int, int, int, int, int, int]] = {}
    try:
        directories = [(root, "BACKUP")]
        if staging.exists():
            if staging.is_symlink() or not staging.is_dir():
                raise OSError("recovery-staging directory is unsafe")
            directories.append((staging, "STAGED"))
        for directory, artifact_type in directories:
            assert_managed_directories_unchanged()
            for path in sorted(directory.iterdir(), key=lambda item: item.name):
                assert_managed_directories_unchanged()
                if path == staging and artifact_type == "BACKUP":
                    continue
                if path.is_dir() and not path.is_symlink():
                    continue
                relevant = (
                    (artifact_type == "BACKUP" and path.name.startswith("taimobot-") and path.name.endswith(".sqlite3"))
                    or (artifact_type == "STAGED" and path.name.endswith(".sqlite3"))
                    or path.name.startswith(".taimobot-")
                )
                if not relevant:
                    continue
                entry_metadata = path.stat(follow_symlinks=False)
                artifact_snapshots[(str(directory), artifact_type, path.name)] = (
                    entry_metadata.st_dev, entry_metadata.st_ino, entry_metadata.st_mode,
                    entry_metadata.st_size, entry_metadata.st_mtime_ns,
                    entry_metadata.st_ctime_ns, entry_metadata.st_nlink,
                )
                item = {
                    "file_name": path.name,
                    "artifact_type": artifact_type if path.name.endswith(".sqlite3") else "INCOMPLETE_TEMPORARY",
                    "references": [],
                    "issues": [],
                    "sha256": None,
                    "integrity_check": "not_checked",
                }
                if path.is_symlink() or not path.is_file():
                    item["status"] = "UNSAFE"
                    item["issues"].append("NOT_A_REGULAR_NON_SYMLINK_FILE")
                    artifacts.append(item)
                    continue
                if path.resolve() == Path(database.DB_PATH).resolve():
                    item["status"] = "PROTECTED_LIVE_DATABASE"
                    item["issues"].append("LIVE_DATABASE_IS_NOT_A_BACKUP_ARTIFACT")
                    artifacts.append(item)
                    continue
                path_metadata = entry_metadata
                inode = (path_metadata.st_dev, path_metadata.st_ino)
                previous = seen_inodes.get(inode)
                if previous:
                    item["issues"].append("HARD_LINK_ALIAS")
                    issues.append("DUPLICATE_ARTIFACT_IDENTITY")
                else:
                    seen_inodes[inode] = str(path)
                resolved = str(path.resolve())
                item["references"] = references.get(resolved, [])
                if not path.name.endswith(".sqlite3"):
                    item["status"] = "INCOMPLETE"
                    item["issues"].append("TEMPORARY_BACKUP_ARTIFACT")
                    artifacts.append(item)
                    continue
                try:
                    file_before = path.stat(follow_symlinks=False)
                    item["sha256"] = sha256_file(path)
                    assert_managed_directories_unchanged()
                    with sqlite3.connect(
                        f"file:{path.resolve()}?mode=ro", uri=True, timeout=10
                    ) as check:
                        row = check.execute("PRAGMA integrity_check").fetchone()
                    item["integrity_check"] = row[0] if row else "no_result"
                    file_after = path.stat(follow_symlinks=False)
                    if (
                        path.is_symlink()
                        or not stat.S_ISREG(file_after.st_mode)
                        or file_after.st_dev != file_before.st_dev
                        or file_after.st_ino != file_before.st_ino
                        or file_after.st_size != file_before.st_size
                        or file_after.st_mtime_ns != file_before.st_mtime_ns
                    ):
                        item["issues"].append("ARTIFACT_CHANGED_DURING_INVENTORY")
                        issues.append("ARTIFACT_CHANGED_DURING_INVENTORY")
                    assert_managed_directories_unchanged()
                    if item["integrity_check"] != "ok":
                        item["issues"].append("SQLITE_INTEGRITY_FAILED")
                except (sqlite3.Error, OSError, ValueError):
                    item["issues"].append("ARTIFACT_UNREADABLE")

                roles = {
                    reference.get("source", "").rsplit(".", 1)[-1]
                    for reference in item["references"]
                    if reference.get("plan_id")
                }
                for reference in item["references"]:
                    plan_id = reference.get("plan_id")
                    source = reference.get("source", "")
                    role = source.rsplit(".", 1)[-1]
                    if plan_id and role in ("source", "staged", "rollback"):
                        expected = expected_hashes.get((str(plan_id), role))
                        if expected and item["sha256"] != expected:
                            item["issues"].append("RESTORE_PLAN_FINGERPRINT_MISMATCH")
                            issues.append("RESTORE_PLAN_FINGERPRINT_MISMATCH")
                if item["issues"]:
                    item["status"] = "REVIEW_REQUIRED"
                elif not item["references"]:
                    item["status"] = "ORPHANED"
                else:
                    item["status"] = "REFERENCED"
                artifacts.append(item)
    except (OSError, ValueError) as error:
        return {
            **result_base,
            "status": "RECOVERY_ARTIFACT_INVENTORY_BLOCKED",
            "journal_status": journal_verification.get("status"),
            "artifacts": artifacts,
            "orphaned_artifacts": [],
            "missing_references": missing_references,
            "issues": list(dict.fromkeys(issues + ["ARTIFACT_DIRECTORY_UNREADABLE", str(error)[:180]])),
        }

    try:
        assert_managed_directories_unchanged()
    except OSError as error:
        return {
            **result_base,
            "status": "RECOVERY_ARTIFACT_INVENTORY_BLOCKED",
            "journal_status": journal_verification.get("status"),
            "artifacts": artifacts,
            "orphaned_artifacts": [],
            "missing_references": missing_references,
            "issues": list(dict.fromkeys(issues + ["ARTIFACT_DIRECTORY_CHANGED_DURING_INVENTORY", str(error)[:180]])),
        }

    present = {
        str((directory / item["file_name"]).resolve())
        for directory, kind in directories
        for item in artifacts
        if item["artifact_type"] != "INCOMPLETE_TEMPORARY"
        and item["artifact_type"] == kind
    }
    for key, refs in references.items():
        if key not in present:
            for reference in refs:
                missing_references.append({
                    **reference, "path": key, "status": "REFERENCED_FILE_MISSING",
                })
                issues.append("REFERENCED_RECOVERY_ARTIFACT_MISSING")

    # Re-read maintenance metadata after filesystem inspection. A concurrent
    # plan/run/attempt update must never make an artifact appear orphaned using
    # a stale reference snapshot.
    try:
        with database.get_connection() as connection:
            final_metadata_fingerprint = metadata_state_fingerprint(connection)
    except (sqlite3.Error, OSError, ValueError) as error:
        return {
            **result_base,
            "status": "RECOVERY_ARTIFACT_INVENTORY_BLOCKED",
            "journal_status": journal_verification.get("status"),
            "artifacts": artifacts,
            "orphaned_artifacts": [],
            "missing_references": missing_references,
            "issues": list(dict.fromkeys(issues + ["MAINTENANCE_METADATA_UNREADABLE_DURING_INVENTORY", str(error)[:180]])),
        }
    try:
        if database_file_identity() != initial_database_identity:
            return {
                **result_base,
                "status": "RECOVERY_ARTIFACT_INVENTORY_BLOCKED",
                "journal_status": journal_verification.get("status"),
                "artifacts": artifacts,
                "orphaned_artifacts": [],
                "missing_references": missing_references,
                "issues": list(dict.fromkeys(issues + ["LIVE_DATABASE_IDENTITY_CHANGED_DURING_INVENTORY"])),
            }
    except OSError as error:
        return {
            **result_base,
            "status": "RECOVERY_ARTIFACT_INVENTORY_BLOCKED",
            "journal_status": journal_verification.get("status"),
            "artifacts": artifacts,
            "orphaned_artifacts": [],
            "missing_references": missing_references,
            "issues": list(dict.fromkeys(issues + ["LIVE_DATABASE_IDENTITY_UNSAFE_DURING_INVENTORY", str(error)[:180]])),
        }

    if final_metadata_fingerprint != initial_metadata_fingerprint:
        return {
            **result_base,
            "status": "RECOVERY_ARTIFACT_INVENTORY_BLOCKED",
            "journal_status": journal_verification.get("status"),
            "artifacts": artifacts,
            "orphaned_artifacts": [],
            "missing_references": missing_references,
            "issues": list(dict.fromkeys(issues + ["MAINTENANCE_METADATA_CHANGED_DURING_INVENTORY"])),
        }

    # Re-verify the journal after filesystem inspection so a concurrent append
    # cannot make an artifact appear orphaned based on a stale journal snapshot.
    try:
        final_journal = verify_restore_execution_journal(journal, include_records=True)
    except OSError as error:
        final_journal = {
            "status": "RESTORE_JOURNAL_UNREADABLE",
            "reason": str(error)[:200],
            "record_count": 0,
        }
    if (
        final_journal.get("status") != journal_verification.get("status")
        or final_journal.get("record_count") != journal_verification.get("record_count")
        or final_journal.get("last_record_sha256") != journal_verification.get("last_record_sha256")
    ):
        issues.append("RESTORE_EXECUTION_JOURNAL_CHANGED_DURING_INVENTORY")

    # Revalidate managed directory entries and every scanned file after journal
    # verification, closing the final window for concurrent backup replacement.
    try:
        assert_managed_directories_unchanged()
        for (directory_path, artifact_type, file_name), expected_identity in artifact_snapshots.items():
            managed_directory = Path(directory_path)
            candidate = managed_directory / file_name
            current = candidate.stat(follow_symlinks=False)
            current_identity = (
                current.st_dev, current.st_ino, current.st_mode,
                current.st_size, current.st_mtime_ns,
                current.st_ctime_ns, current.st_nlink,
            )
            if current_identity != expected_identity:
                raise OSError("recovery artifact identity changed after filesystem inspection")
            if stat.S_ISREG(current.st_mode):
                recorded = next(
                    (item.get("sha256") for item in artifacts
                     if item["file_name"] == file_name and item["artifact_type"] == artifact_type),
                    None,
                )
                if recorded and sha256_file(candidate) != recorded:
                    raise OSError("recovery artifact digest changed before inventory response")
    except (OSError, ValueError) as error:
        return {
            **result_base,
            "status": "RECOVERY_ARTIFACT_INVENTORY_BLOCKED",
            "journal_status": final_journal.get("status"),
            "artifacts": artifacts,
            "orphaned_artifacts": [],
            "missing_references": missing_references,
            "issues": list(dict.fromkeys(issues + ["ARTIFACT_CHANGED_DURING_FINAL_CHECK", str(error)[:180]])),
        }

    # Recheck after final journal verification: a metadata write during that
    # verification must not escape the inventory's last consistency check.
    try:
        with database.get_connection() as connection:
            response_metadata_fingerprint = metadata_state_fingerprint(connection)
        if database_file_identity() != initial_database_identity:
            raise sqlite3.DatabaseError("live database identity changed before inventory response")
    except (sqlite3.Error, OSError, ValueError) as error:
        return {
            **result_base,
            "status": "RECOVERY_ARTIFACT_INVENTORY_BLOCKED",
            "journal_status": final_journal.get("status"),
            "artifacts": artifacts,
            "orphaned_artifacts": [],
            "missing_references": missing_references,
            "issues": list(dict.fromkeys(issues + ["MAINTENANCE_METADATA_UNREADABLE_BEFORE_RESPONSE", str(error)[:180]])),
        }
    if response_metadata_fingerprint != initial_metadata_fingerprint:
        return {
            **result_base,
            "status": "RECOVERY_ARTIFACT_INVENTORY_BLOCKED",
            "journal_status": final_journal.get("status"),
            "artifacts": artifacts,
            "orphaned_artifacts": [],
            "missing_references": missing_references,
            "issues": list(dict.fromkeys(issues + ["MAINTENANCE_METADATA_CHANGED_BEFORE_RESPONSE"])),
        }

    # A filesystem mutation can occur while the metadata fingerprint is read.
    # Re-run the artifact fence after that read, then fingerprint metadata again
    # so writes during this second filesystem pass also fail closed.
    try:
        assert_managed_directories_unchanged()
        for (directory_path, artifact_type, file_name), expected_identity in artifact_snapshots.items():
            managed_directory = Path(directory_path)
            candidate = managed_directory / file_name
            current = candidate.stat(follow_symlinks=False)
            current_identity = (
                current.st_dev, current.st_ino, current.st_mode,
                current.st_size, current.st_mtime_ns,
                current.st_ctime_ns, current.st_nlink,
            )
            if current_identity != expected_identity:
                raise OSError("recovery artifact changed during final consistency fence")
            if stat.S_ISREG(current.st_mode):
                recorded = next(
                    (item.get("sha256") for item in artifacts
                     if item["file_name"] == file_name and item["artifact_type"] == artifact_type),
                    None,
                )
                if recorded and sha256_file(candidate) != recorded:
                    raise OSError("recovery artifact digest changed during final consistency fence")
        assert_managed_directories_unchanged()
    except (OSError, ValueError) as error:
        return {
            **result_base,
            "status": "RECOVERY_ARTIFACT_INVENTORY_BLOCKED",
            "journal_status": final_journal.get("status"),
            "artifacts": artifacts,
            "orphaned_artifacts": [],
            "missing_references": missing_references,
            "issues": list(dict.fromkeys(issues + ["ARTIFACT_CHANGED_DURING_FINAL_CHECK", str(error)[:180]])),
        }

    try:
        with database.get_connection() as connection:
            final_metadata_fingerprint = metadata_state_fingerprint(connection)
        if database_file_identity() != initial_database_identity:
            raise sqlite3.DatabaseError("live database identity changed during final consistency fence")
    except (sqlite3.Error, OSError, ValueError) as error:
        return {
            **result_base,
            "status": "RECOVERY_ARTIFACT_INVENTORY_BLOCKED",
            "journal_status": final_journal.get("status"),
            "artifacts": artifacts,
            "orphaned_artifacts": [],
            "missing_references": missing_references,
            "issues": list(dict.fromkeys(issues + ["MAINTENANCE_METADATA_UNREADABLE_BEFORE_RESPONSE", str(error)[:180]])),
        }
    if final_metadata_fingerprint != initial_metadata_fingerprint:
        return {
            **result_base,
            "status": "RECOVERY_ARTIFACT_INVENTORY_BLOCKED",
            "journal_status": final_journal.get("status"),
            "artifacts": artifacts,
            "orphaned_artifacts": [],
            "missing_references": missing_references,
            "issues": list(dict.fromkeys(issues + ["MAINTENANCE_METADATA_CHANGED_BEFORE_RESPONSE"])),
        }

    # The metadata read above is itself a race window for filesystem changes.
    # Revalidate the full managed-file snapshot once more, then take one final
    # metadata fingerprint so changes during this last filesystem pass are caught.
    try:
        assert_managed_directories_unchanged()
        for (directory_path, artifact_type, file_name), expected_identity in artifact_snapshots.items():
            managed_directory = Path(directory_path)
            candidate = managed_directory / file_name
            current = candidate.stat(follow_symlinks=False)
            current_identity = (
                current.st_dev, current.st_ino, current.st_mode,
                current.st_size, current.st_mtime_ns,
                current.st_ctime_ns, current.st_nlink,
            )
            if current_identity != expected_identity:
                raise OSError("recovery artifact changed after final metadata verification")
            if stat.S_ISREG(current.st_mode):
                recorded = next(
                    (item.get("sha256") for item in artifacts
                     if item["file_name"] == file_name and item["artifact_type"] == artifact_type),
                    None,
                )
                if recorded and sha256_file(candidate) != recorded:
                    raise OSError("recovery artifact digest changed after final metadata verification")
        assert_managed_directories_unchanged()
    except (OSError, ValueError) as error:
        return {
            **result_base,
            "status": "RECOVERY_ARTIFACT_INVENTORY_BLOCKED",
            "journal_status": final_journal.get("status"),
            "artifacts": artifacts,
            "orphaned_artifacts": [],
            "missing_references": missing_references,
            "issues": list(dict.fromkeys(issues + ["ARTIFACT_CHANGED_DURING_FINAL_CHECK", str(error)[:180]])),
        }

    try:
        with database.get_connection() as connection:
            terminal_metadata_fingerprint = metadata_state_fingerprint(connection)
        if database_file_identity() != initial_database_identity:
            raise sqlite3.DatabaseError("live database identity changed during terminal consistency fence")
    except (sqlite3.Error, OSError, ValueError) as error:
        return {
            **result_base,
            "status": "RECOVERY_ARTIFACT_INVENTORY_BLOCKED",
            "journal_status": final_journal.get("status"),
            "artifacts": artifacts,
            "orphaned_artifacts": [],
            "missing_references": missing_references,
            "issues": list(dict.fromkeys(issues + ["MAINTENANCE_METADATA_UNREADABLE_BEFORE_RESPONSE", str(error)[:180]])),
        }
    if terminal_metadata_fingerprint != initial_metadata_fingerprint:
        return {
            **result_base,
            "status": "RECOVERY_ARTIFACT_INVENTORY_BLOCKED",
            "journal_status": final_journal.get("status"),
            "artifacts": artifacts,
            "orphaned_artifacts": [],
            "missing_references": missing_references,
            "issues": list(dict.fromkeys(issues + ["MAINTENANCE_METADATA_CHANGED_BEFORE_RESPONSE"])),
        }

    # The terminal metadata read is another filesystem race window. Recheck
    # every managed artifact after it, so a replacement or content mutation
    # during that read cannot be returned as a clean inventory.
    try:
        assert_managed_directories_unchanged()
        for (directory_path, artifact_type, file_name), expected_identity in artifact_snapshots.items():
            candidate = Path(directory_path) / file_name
            current = candidate.stat(follow_symlinks=False)
            current_identity = (
                current.st_dev, current.st_ino, current.st_mode,
                current.st_size, current.st_mtime_ns,
                current.st_ctime_ns, current.st_nlink,
            )
            if current_identity != expected_identity:
                raise OSError("recovery artifact changed after terminal metadata verification")
            if stat.S_ISREG(current.st_mode):
                recorded = next(
                    (item.get("sha256") for item in artifacts
                     if item["file_name"] == file_name and item["artifact_type"] == artifact_type),
                    None,
                )
                if recorded and sha256_file(candidate) != recorded:
                    raise OSError("recovery artifact digest changed after terminal metadata verification")
                # Hashing is not the final observation: an external process can
                # replace or mutate the path immediately after sha256_file returns.
                post_hash = candidate.stat(follow_symlinks=False)
                post_hash_identity = (
                    post_hash.st_dev, post_hash.st_ino, post_hash.st_mode,
                    post_hash.st_size, post_hash.st_mtime_ns,
                    post_hash.st_ctime_ns, post_hash.st_nlink,
                )
                if candidate.is_symlink() or post_hash_identity != expected_identity:
                    raise OSError("recovery artifact changed immediately after final digest verification")
        assert_managed_directories_unchanged()
    except (OSError, ValueError) as error:
        return {
            **result_base,
            "status": "RECOVERY_ARTIFACT_INVENTORY_BLOCKED",
            "journal_status": final_journal.get("status"),
            "artifacts": artifacts,
            "orphaned_artifacts": [],
            "missing_references": missing_references,
            "issues": list(dict.fromkeys(issues + ["ARTIFACT_CHANGED_DURING_FINAL_CHECK", str(error)[:180]])),
        }

    orphaned = [item for item in artifacts if item.get("status") == "ORPHANED"]
    blocked = bool(issues) or any(item.get("status") in ("UNSAFE", "REVIEW_REQUIRED", "INCOMPLETE", "PROTECTED_LIVE_DATABASE") for item in artifacts)
    return {
        **result_base,
        "status": "RECOVERY_ARTIFACT_INVENTORY_REVIEW_REQUIRED" if blocked or orphaned else "RECOVERY_ARTIFACT_INVENTORY_COMPLETE",
        "journal_status": journal_verification.get("status"),
        "database_plan_count": len(database_plan_ids),
        "journal_plan_count": len(journal_plan_ids),
        "artifact_count": len(artifacts),
        "orphaned_count": len(orphaned),
        "artifacts": artifacts,
        "orphaned_artifacts": orphaned,
        "missing_references": missing_references,
        "issues": list(dict.fromkeys(issues)),
    }


def inspect_recovery_candidates(backup_dir: Path | None = None) -> dict:
    """Read-only inventory of managed backups with integrity and schema checks.

    A file is restore-ready only when SQLite integrity, essential TaimoBot
    tables/columns, and foreign-key checks all pass. This does not restore or
    modify any database.
    """
    directory = Path(backup_dir or BACKUP_DIR)
    if not directory.exists():
        return {"status": "NO_BACKUP_DIRECTORY", "candidates": []}
    if not directory.is_dir():
        return {"status": "RECOVERY_INVENTORY_BLOCKED", "candidates": []}

    required_columns = {
        "users": {"user_id", "email", "password_hash", "created_at"},
        "paper_accounts": {"account_id", "starting_balance", "balance"},
        "trades": {
            "trade_id", "account_id", "client_order_id", "symbol",
            "side", "quantity", "entry_price", "stop_loss", "status",
            "risk_amount", "realized_pnl",
        },
        "position_groups": {"group_id", "account_id", "symbol", "side", "status"},
        "copy_relationships": {
            "relationship_id", "source_account_id", "follower_account_id", "status",
        },
        "copy_executions": {
            "copy_execution_id", "relationship_id", "source_trade_id", "status",
        },
        "participant_milestones": {"user_id", "joined_at", "first_trade_at"},
        "system_safety_state": {"singleton_id", "emergency_stop_active"},
    }

    try:
        entries = list(directory.iterdir())
    except OSError:
        return {"status": "RECOVERY_INVENTORY_BLOCKED", "candidates": []}

    candidates = []
    for path in entries:
        if not path.name.startswith("taimobot-") or not path.name.endswith(".sqlite3"):
            continue
        if path.is_symlink() or not path.is_file():
            continue

        issues = []
        integrity = "not_checked"
        missing_schema = []
        foreign_key_violations = None
        try:
            file_stat = path.stat(follow_symlinks=False)
            sidecars = inspect_sqlite_sidecars(path)
            if sidecars["status"] != "SIDECARS_CLEAR":
                issues.append("SQLITE_SIDECARS_PRESENT")
                candidates.append({
                    "file_name": path.name,
                    "size_bytes": file_stat.st_size,
                    "modified_at": datetime.fromtimestamp(file_stat.st_mtime, SAST).isoformat(),
                    "sha256": sha256_file(path),
                    "integrity_check": "SKIPPED_SIDECARS_PRESENT",
                    "missing_schema": [],
                    "foreign_key_violations": None,
                    "sidecars": sidecars,
                    "restore_ready": False,
                    "issues": issues,
                })
                continue
            with sqlite3.connect(
                f"file:{path.resolve()}?mode=ro", uri=True, timeout=10
            ) as connection:
                result = connection.execute("PRAGMA integrity_check").fetchone()
                integrity = result[0] if result else "no_result"
                if integrity != "ok":
                    issues.append("SQLITE_INTEGRITY_FAILED")

                tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
                for table, expected_columns in required_columns.items():
                    if table not in tables:
                        missing_schema.append(table)
                        continue
                    actual_columns = {
                        row[1] for row in connection.execute(
                            f"PRAGMA table_info({table})"
                        )
                    }
                    missing = expected_columns - actual_columns
                    if missing:
                        missing_schema.append(
                            f"{table}:" + ",".join(sorted(missing))
                        )
                if missing_schema:
                    issues.append("TAIMOBOT_SCHEMA_INCOMPLETE")

                foreign_key_violations = len(
                    connection.execute("PRAGMA foreign_key_check").fetchall()
                )
                if foreign_key_violations:
                    issues.append("FOREIGN_KEY_VIOLATIONS")

            candidates.append({
                "file_name": path.name,
                "size_bytes": file_stat.st_size,
                "modified_at": datetime.fromtimestamp(file_stat.st_mtime, SAST).isoformat(),
                "sha256": sha256_file(path),
                "integrity_check": integrity,
                "missing_schema": missing_schema,
                "foreign_key_violations": foreign_key_violations,
                "sidecars": sidecars,
                "restore_ready": not issues,
                "issues": issues,
            })
        except (sqlite3.Error, OSError, ValueError):
            candidates.append({
                "file_name": path.name,
                "integrity_check": integrity,
                "missing_schema": missing_schema,
                "foreign_key_violations": foreign_key_violations,
                "restore_ready": False,
                "issues": ["BACKUP_UNREADABLE"],
            })

    candidates.sort(
        key=lambda item: (item.get("modified_at", ""), item["file_name"]),
        reverse=True,
    )
    return {
        "status": "RECOVERY_INVENTORY_COMPLETE",
        "candidate_count": len(candidates),
        "restore_ready_count": sum(1 for item in candidates if item["restore_ready"]),
        "candidates": candidates,
        "live_database_replaced": False,
    }


def verify_restore_candidate(backup_path: Path, staging_path: Path) -> dict:
    """Verify a descriptor-pinned source snapshot in isolated staging; never replace live DB."""
    source = Path(backup_path)
    staging = Path(staging_path)
    if source.resolve(strict=False) == staging.resolve(strict=False):
        raise ValueError("Restore source and staging paths must be different.")
    staging.parent.mkdir(parents=True, exist_ok=True)
    staging.unlink(missing_ok=True)

    # Keep the private source snapshot outside the managed backup/staging tree.
    # Its TemporaryDirectory remains addressable even if that tree is renamed.
    snapshot_directory = tempfile.TemporaryDirectory(prefix="taimobot-restore-source-")
    snapshot = Path(snapshot_directory.name) / "source.sqlite3"
    source_fd = None
    snapshot_fd = None

    def file_identity(metadata):
        return (
            metadata.st_dev, metadata.st_ino, metadata.st_mode,
            metadata.st_nlink, metadata.st_size,
            metadata.st_mtime_ns, metadata.st_ctime_ns,
        )

    try:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        source_fd = os.open(str(source), flags)
        source_before = os.fstat(source_fd)
        if not stat.S_ISREG(source_before.st_mode):
            raise OSError("Restore source is not a regular non-symlink file.")
        source_identity = file_identity(source_before)
        path_before = os.stat(source, follow_symlinks=False)
        if file_identity(path_before) != source_identity:
            raise OSError("Restore source path changed before snapshot.")

        snapshot_flags = (
            os.O_WRONLY | os.O_CREAT | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        snapshot_fd = os.open(str(snapshot), snapshot_flags, 0o600)
        bytes_copied = 0
        while True:
            chunk = os.read(source_fd, 1024 * 1024)
            if not chunk:
                break
            view = memoryview(chunk)
            while view:
                written = os.write(snapshot_fd, view)
                if written <= 0:
                    raise OSError("Restore source snapshot write made no progress.")
                view = view[written:]
            bytes_copied += len(chunk)
        os.fsync(snapshot_fd)

        source_after = os.fstat(source_fd)
        path_after = os.stat(source, follow_symlinks=False)
        if (
            file_identity(source_after) != source_identity
            or file_identity(path_after) != source_identity
            or bytes_copied != source_before.st_size
        ):
            raise OSError("Restore source changed while its isolated snapshot was created.")

        os.close(snapshot_fd)
        snapshot_fd = None
        snapshot_digest = sha256_file(snapshot)

        # SQLite reads only the private, descriptor-derived snapshot. The
        # caller's source path is never reopened by SQLite during staging.
        with sqlite3.connect(str(snapshot), timeout=30) as backup:
            with sqlite3.connect(str(staging), timeout=30) as candidate:
                backup.backup(candidate)
        if sha256_file(snapshot) != snapshot_digest:
            raise OSError("Isolated restore source snapshot changed during SQLite verification.")

        source_after_sqlite = os.fstat(source_fd)
        path_after_sqlite = os.stat(source, follow_symlinks=False)
        if (
            file_identity(source_after_sqlite) != source_identity
            or file_identity(path_after_sqlite) != source_identity
        ):
            raise OSError("Restore source changed while SQLite verified the isolated snapshot.")

        with sqlite3.connect(str(staging), timeout=30) as candidate:
            integrity = candidate.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok":
                raise RuntimeError(f"Restore candidate failed integrity check: {integrity}")
    except Exception:
        staging.unlink(missing_ok=True)
        raise
    finally:
        if snapshot_fd is not None:
            os.close(snapshot_fd)
        if source_fd is not None:
            os.close(source_fd)
        snapshot_directory.cleanup()

    return {
        "status": "RESTORE_CANDIDATE_VERIFIED",
        "staging_file": str(staging),
        "integrity_check": "ok",
        "live_database_replaced": False,
    }


def sha256_file(path: Path, *, chunk_size: int = 1024 * 1024) -> str:
    """Return a streaming SHA-256 digest for a managed recovery artifact."""
    if isinstance(chunk_size, bool) or not isinstance(chunk_size, int) or chunk_size < 1:
        raise ValueError("chunk_size must be a positive integer")
    digest = hashlib.sha256()
    artifact_path = Path(path)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(str(artifact_path), flags)
    try:
        metadata_before = os.fstat(descriptor)
        if not stat.S_ISREG(metadata_before.st_mode):
            raise OSError("Recovery artifact is not a regular file.")
        def identity(value):
            return (
                value.st_dev, value.st_ino, value.st_mode, value.st_nlink,
                value.st_size, value.st_mtime_ns, value.st_ctime_ns,
            )
        initial_identity = identity(metadata_before)
        bytes_read = 0
        with os.fdopen(descriptor, "rb", closefd=False) as artifact:
            while True:
                chunk = artifact.read(chunk_size)
                if not chunk:
                    break
                digest.update(chunk)
                bytes_read += len(chunk)
        metadata_after = os.fstat(descriptor)
        try:
            path_after = os.stat(artifact_path, follow_symlinks=False)
        except OSError as error:
            raise OSError("Recovery artifact path changed while its digest was computed.") from error
        if (
            identity(metadata_after) != initial_identity
            or identity(path_after) != initial_identity
            or bytes_read != metadata_before.st_size
        ):
            raise OSError("Recovery artifact changed while its digest was computed.")
    finally:
        os.close(descriptor)
    return digest.hexdigest()



def restore_lock_path() -> Path:
    """Return the process-shared lock path adjacent to the live database."""
    return Path(database.DB_PATH).with_name(Path(database.DB_PATH).name + ".restore.lock")


def is_restore_lock_active() -> bool:
    """Fail closed if a restore lock exists or cannot be safely inspected."""
    lock_path = restore_lock_path()
    try:
        return lock_path.exists() or lock_path.is_symlink()
    except OSError:
        return True


def acquire_restore_lock(plan_id: str, *, timeout_seconds: float = 1.0) -> dict:
    """Block cooperating app clients without mutating SQLite or its sidecars.

    The exclusive OS guard coordinates only TaimoBot clients using the guard
    protocol. It cannot prove that arbitrary external SQLite processes have
    stopped, so callers must not treat this lock as full database quiescence.
    The OS lock is acquired before publishing the marker to avoid exposing an
    abandoned-looking marker while its owner is still waiting for the guard.
    """
    if not isinstance(plan_id, str) or not re.fullmatch(r"[a-f0-9]{32}", plan_id):
        raise ValueError("A valid restore plan ID is required.")
    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)) or timeout_seconds < 0:
        raise ValueError("timeout_seconds must be a non-negative number.")

    lock_path = restore_lock_path()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    token = uuid4().hex
    payload = {
        "plan_id": plan_id,
        "lock_token": token,
        "pid": os.getpid(),
        "created_at": datetime.now(SAST).isoformat(),
    }
    guard_fd = None
    descriptor = None
    try:
        with database._CONNECTION_GUARD:
            if database.open_connection_count():
                raise RuntimeError(
                    "Restore lock refused: application database connections remain open."
                )
            guard_path = database.restore_guard_path()
            guard_fd = os.open(
                str(guard_path),
                os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            if not stat.S_ISREG(os.fstat(guard_fd).st_mode):
                raise RuntimeError("Restore guard is not a regular file; lock acquisition refused.")
            try:
                guard_path_stat = guard_path.lstat()
            except OSError as error:
                raise RuntimeError("Restore guard path cannot be verified safely.") from error
            guard_fd_stat = os.fstat(guard_fd)
            if (
                stat.S_ISLNK(guard_path_stat.st_mode)
                or (guard_path_stat.st_dev, guard_path_stat.st_ino)
                != (guard_fd_stat.st_dev, guard_fd_stat.st_ino)
            ):
                raise RuntimeError("Restore guard changed during acquisition; lock refused.")
            deadline = time.monotonic() + float(timeout_seconds)
            while True:
                try:
                    fcntl.flock(guard_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise RuntimeError(
                            "Restore lock refused: database connections remain active in another process."
                        )
                    time.sleep(0.05)

            descriptor = os.open(
                str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
            )
            with os.fdopen(descriptor, "w", encoding="utf-8") as lock_file:
                descriptor = None
                lock_file.write(json.dumps(payload, sort_keys=True))
                lock_file.flush()
                os.fsync(lock_file.fileno())

            # Persist the directory entry as well as the marker contents before
            # reporting acquisition. A file fsync alone does not guarantee the
            # newly-created name survives a host crash.
            marker_directory_fd = os.open(
                str(lock_path.parent),
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
            )
            try:
                os.fsync(marker_directory_fd)
            finally:
                os.close(marker_directory_fd)

        _RESTORE_GUARD_FDS[token] = guard_fd
        guard_fd = None

        # Inventory sidecars only. In particular, do not run WAL checkpoint:
        # sidecar preservation/handling belongs to a separately tested restore
        # protocol and must never be an implicit side effect of lock acquisition.
        sidecars = inspect_sqlite_sidecars(database.DB_PATH)
        return {
            "status": "RESTORE_LOCK_ACQUIRED",
            "plan_id": plan_id,
            "lock_token": token,
            "application_connections_quiescent": True,
            "external_process_quiescence_verified": False,
            "database_quiescent": False,
            "sqlite_sidecars": sidecars,
            "lock_file": str(lock_path),
        }
    except Exception:
        if descriptor is not None:
            os.close(descriptor)
        if guard_fd is not None:
            os.close(guard_fd)
        held_fd = _RESTORE_GUARD_FDS.pop(token, None)
        if held_fd is not None:
            os.close(held_fd)
        try:
            if lock_path.is_file() and not lock_path.is_symlink():
                current = json.loads(lock_path.read_text(encoding="utf-8"))
                if current.get("lock_token") == token:
                    lock_path.unlink()
                    cleanup_directory_fd = os.open(
                        str(lock_path.parent),
                        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
                    )
                    try:
                        os.fsync(cleanup_directory_fd)
                    finally:
                        os.close(cleanup_directory_fd)
        except (OSError, ValueError, json.JSONDecodeError):
            # An uncertain cleanup remains fail-closed: a surviving marker
            # blocks new restore work and requires operator inspection.
            pass
        raise


def inspect_restore_lock() -> dict:
    """Read-only inspection of a restore marker and its process-shared guard.

    Marker and guard identities are checked around the OS-lock probe. Any
    concurrent replacement or mutation is reported as inconclusive; this
    function never removes or rewrites a marker.
    """
    lock_path = restore_lock_path()
    marker_fd = None
    guard_fd = None
    try:
        marker_fd = os.open(
            str(lock_path), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        )
        marker_stat = os.fstat(marker_fd)
        if not __import__("stat").S_ISREG(marker_stat.st_mode):
            return {"status": "RESTORE_LOCK_UNSAFE_MARKER", "marker_present": True}
        marker_identity = (
            marker_stat.st_dev, marker_stat.st_ino,
            marker_stat.st_size, marker_stat.st_mtime_ns,
        )
        marker_bytes = os.read(marker_fd, 65536)
        if os.read(marker_fd, 1):
            raise ValueError("restore marker exceeds inspection limit")
    except FileNotFoundError:
        return {"status": "NO_RESTORE_LOCK", "marker_present": False}
    except OSError as error:
        status = (
            "RESTORE_LOCK_UNSAFE_MARKER"
            if getattr(error, "errno", None) in (getattr(os, "ELOOP", 40),)
            else "RESTORE_LOCK_UNINSPECTABLE"
        )
        return {"status": status, "marker_present": True}
    except ValueError:
        return {"status": "RESTORE_LOCK_METADATA_UNREADABLE", "marker_present": True}

    try:
        payload = json.loads(
            marker_bytes.decode("utf-8"),
            object_pairs_hook=_restore_journal_object_without_duplicate_keys,
            parse_constant=_reject_restore_journal_json_constant,
        )
        if not isinstance(payload, dict):
            raise ValueError("restore marker must be a JSON object")
        plan_id = payload.get("plan_id")
        process_id = payload.get("pid")
        created_at = payload.get("created_at")
        lock_token = payload.get("lock_token")
        if (
            not isinstance(plan_id, str)
            or not re.fullmatch(r"[a-f0-9]{32}", plan_id)
            or (
                lock_token is not None
                and (
                    not isinstance(lock_token, str)
                    or not re.fullmatch(r"[a-f0-9]{32}", lock_token)
                )
            )
            or isinstance(process_id, bool)
            or not isinstance(process_id, int)
            or process_id <= 0
            or not isinstance(created_at, str)
        ):
            raise ValueError("invalid restore marker metadata")
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
        os.close(marker_fd)
        return {"status": "RESTORE_LOCK_METADATA_UNREADABLE", "marker_present": True}

    guard_path = database.restore_guard_path()
    try:
        try:
            guard_stat = guard_path.lstat()
            if not __import__("stat").S_ISREG(guard_stat.st_mode):
                return {"status": "RESTORE_LOCK_UNSAFE_GUARD", "marker_present": True}
        except FileNotFoundError:
            pass
        guard_fd = os.open(
            str(guard_path),
            os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        if not __import__("stat").S_ISREG(os.fstat(guard_fd).st_mode):
            return {"status": "RESTORE_LOCK_UNSAFE_GUARD", "marker_present": True}
        try:
            fcntl.flock(guard_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            status = "ABANDONED_RESTORE_MARKER_REQUIRES_OPERATOR_REVIEW"
        except BlockingIOError:
            status = "RESTORE_LOCK_ACTIVE"

        # Re-check both the pathname identity and bytes after the lock probe.
        # This prevents a concurrent marker swap/edit from being labelled stale.
        current_stat = lock_path.lstat()
        current_identity = (
            current_stat.st_dev, current_stat.st_ino,
            current_stat.st_size, current_stat.st_mtime_ns,
        )
        os.lseek(marker_fd, 0, os.SEEK_SET)
        current_bytes = os.read(marker_fd, 65536)
        if os.read(marker_fd, 1):
            current_bytes = b""
        if current_identity != marker_identity or current_bytes != marker_bytes:
            status = "RESTORE_LOCK_CHANGED_DURING_INSPECTION"
    except FileNotFoundError:
        status = "RESTORE_LOCK_CHANGED_DURING_INSPECTION"
    except OSError:
        status = "RESTORE_LOCK_GUARD_UNINSPECTABLE"
    finally:
        if guard_fd is not None:
            os.close(guard_fd)
        os.close(marker_fd)

    return {
        "status": status,
        "marker_present": True,
        "plan_id": plan_id,
        "lock_token": lock_token,
        "process_id": process_id,
        "created_at": created_at,
        "marker_size_bytes": marker_stat.st_size,
        "automatic_recovery_performed": False,
        "marker_removed": False,
    }



_RESTORE_LOCK_RECOVERY_AUDIT_MAX_BYTES = 4 * 1024 * 1024


def _restore_lock_recovery_record_digest(record: dict) -> str:
    payload = {key: value for key, value in record.items() if key != "record_sha256"}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _verify_restore_lock_recovery_audit_fd(descriptor: int, *, allow_unmatched_started: bool = False) -> dict:
    """Verify the recovery audit hash chain from an already-open file descriptor."""
    metadata = os.fstat(descriptor)
    if not stat.S_ISREG(metadata.st_mode):
        raise RuntimeError("Recovery audit destination is not a regular file.")
    if metadata.st_size > _RESTORE_LOCK_RECOVERY_AUDIT_MAX_BYTES:
        raise RuntimeError("Restore lock recovery audit exceeds the verification size limit.")
    raw = os.pread(descriptor, metadata.st_size, 0)
    if len(raw) != metadata.st_size:
        raise RuntimeError("Restore lock recovery audit changed or was truncated during verification.")
    if not raw:
        return {
            "record_count": 0,
            "last_record_sha256": "0" * 64,
            "pending_recovery": None,
        }
    if not raw.endswith(b"\n"):
        raise RuntimeError("Restore lock recovery audit has an incomplete final record.")
    previous = "0" * 64
    count = 0
    records = []
    try:
        for line in raw.splitlines():
            record = json.loads(line.decode("utf-8"), object_pairs_hook=_restore_journal_object_without_duplicate_keys, parse_constant=_reject_restore_journal_json_constant)
            if not isinstance(record, dict) or isinstance(record.get("sequence"), bool) or record.get("sequence") != count + 1:
                raise ValueError("invalid recovery audit sequence")
            if record.get("previous_record_sha256") != previous:
                raise ValueError("recovery audit hash-chain link mismatch")
            if record.get("event") not in ("RESTORE_LOCK_RECOVERY_STARTED", "RESTORE_LOCK_RECOVERY_COMPLETE"):
                raise ValueError("unknown recovery audit event")
            for field in ("operator_id", "incident_ref", "plan_id", "marker_sha256", "marker_file", "timestamp"):
                value = record.get(field)
                if not isinstance(value, str) or not value.strip():
                    raise ValueError(f"invalid recovery audit {field}")
            if not re.fullmatch(r"[a-f0-9]{32}", record["plan_id"]) or not re.fullmatch(r"[a-f0-9]{64}", record["marker_sha256"]):
                raise ValueError("invalid recovery audit identity or fingerprint")
            if record.get("database_changed") is not False or record.get("sidecars_changed") is not False:
                raise ValueError("recovery audit cannot claim database or sidecar changes")
            parsed = datetime.fromisoformat(record["timestamp"])
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                raise ValueError("recovery audit timestamp must include a timezone offset")
            digest = _restore_lock_recovery_record_digest(record)
            if record.get("record_sha256") != digest:
                raise ValueError("recovery audit record fingerprint mismatch")
            previous = digest
            records.append(record)
            count += 1

        # Recovery is a two-event transaction. A STARTED without COMPLETE is
        # an interrupted operation and must never be silently resumed by a
        # later cleanup attempt. COMPLETE must close the exact same identity.
        pending = None
        identity_fields = ("operator_id", "incident_ref", "plan_id", "marker_sha256", "marker_file")
        for record in records:
            event = record["event"]
            if event == "RESTORE_LOCK_RECOVERY_STARTED":
                if pending is not None:
                    raise ValueError("duplicate STARTED before prior recovery COMPLETE")
                pending = record
            else:
                if pending is None:
                    raise ValueError("COMPLETE event has no matching STARTED")
                if any(record.get(field) != pending.get(field) for field in identity_fields):
                    raise ValueError("recovery COMPLETE identity does not match STARTED")
                pending = None
        if pending is not None and not allow_unmatched_started:
            raise ValueError("unmatched STARTED recovery requires operator review")
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError, RecursionError) as error:
        raise RuntimeError(f"Restore lock recovery audit integrity verification failed: {str(error)[:160]}") from error
    pending_summary = None
    if pending is not None:
        pending_summary = {
            field: pending[field]
            for field in ("operator_id", "incident_ref", "plan_id", "marker_sha256", "marker_file", "timestamp")
        }
    return {
        "record_count": count,
        "last_record_sha256": previous,
        "pending_recovery": pending_summary,
    }


def inspect_restore_recovery_consistency(
    audit_path: Path | None = None, journal_path: Path | None = None
) -> dict:
    """Correlate recovery audit, restore marker and execution journal read-only.

    This function provides operator guidance only. It never resumes recovery,
    removes markers, repairs journals, or changes database files.
    """
    audit = inspect_restore_lock_recovery_audit(audit_path)
    execution = inspect_restore_execution_state(journal_path)
    marker = inspect_restore_lock()
    reasons = []
    decision = "NO_ACTION"
    if audit.get("status") in ("RECOVERY_AUDIT_INVALID", "RECOVERY_AUDIT_UNREADABLE", "RECOVERY_AUDIT_BUSY"):
        reasons.append("Recovery audit is invalid, unreadable, or busy.")
    if execution.get("status") in ("RESTORE_EXECUTION_INSPECTION_BLOCKED", "RESTORE_JOURNAL_SEMANTIC_INVALID"):
        reasons.append("Restore execution journal cannot be trusted.")
    if marker.get("status") not in ("NO_RESTORE_LOCK", "RESTORE_LOCK_ACTIVE",
                                     "ABANDONED_RESTORE_MARKER_REQUIRES_OPERATOR_REVIEW"):
        reasons.append("Restore marker is missing, unsafe, or inconclusive.")
    pending = audit.get("pending_recovery")
    if pending:
        if audit.get("marker_state") != "MATCHES_INTERRUPTED_RECOVERY":
            reasons.append("Interrupted recovery marker is absent or does not match its recorded fingerprint.")
        if marker.get("plan_id") and marker.get("plan_id") != pending.get("plan_id"):
            reasons.append("Restore marker plan ID conflicts with the interrupted recovery audit.")
        reasons.append("Recovery has an unmatched STARTED event; do not retry or complete it automatically.")
    journal_executions = execution.get("executions", [])
    if pending and any(item.get("plan_id") == pending.get("plan_id") for item in journal_executions):
        reasons.append("The same plan also appears in the restore execution journal; reconcile both histories manually.")
    if any(not item.get("terminal") for item in journal_executions):
        reasons.append("A restore execution has no terminal journal event.")
    if execution.get("status") in ("RESTORE_EXECUTION_REVIEW_REQUIRED", "RESTORE_EXECUTION_TERMINAL_EVENTS_RECORDED"):
        if journal_executions:
            reasons.append("Execution journal records require artifact/database reconciliation before any further restore.")
    if reasons:
        decision = "OPERATOR_REVIEW_REQUIRED"
    return {
        "status": "RESTORE_RECOVERY_CONSISTENCY_REVIEW" if reasons else "RESTORE_RECOVERY_CONSISTENT",
        "decision": decision,
        "recommended_action": "DO_NOT_RETRY_OR_MUTATE; REVIEW_AUDIT_MARKER_AND_JOURNAL" if reasons else "NO_ACTION",
        "reasons": list(dict.fromkeys(reasons)),
        "recovery_audit": audit,
        "execution_journal": execution,
        "restore_marker": marker,
        "read_only": True,
        "automatic_repair": False,
        "automatic_resume": False,
        "database_changed": False,
        "sidecars_changed": False,
    }


def inspect_restore_lock_recovery_audit(audit_path: Path | None = None) -> dict:
    """Read-only inspection of recovery audit consistency and any interrupted transaction.

    This diagnostic never creates, truncates, repairs, or appends to the audit,
    restore marker, database, or SQLite sidecars.
    """
    audit_file = Path(
        audit_path
        or (Path(database.DB_PATH).parent / "restore-lock-recovery.audit.jsonl")
    )
    try:
        descriptor = os.open(str(audit_file), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return {
            "status": "NO_RECOVERY_AUDIT",
            "audit_file": str(audit_file),
            "record_count": 0,
            "pending_recovery": None,
            "marker_state": "NOT_CHECKED",
            "read_only": True,
            "automatic_repair": False,
        }
    except OSError as error:
        return {
            "status": "RECOVERY_AUDIT_UNREADABLE",
            "audit_file": str(audit_file),
            "error": str(error)[:160],
            "read_only": True,
            "automatic_repair": False,
        }

    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise RuntimeError("Recovery audit destination is not a regular file.")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return {
                "status": "RECOVERY_AUDIT_BUSY",
                "audit_file": str(audit_file),
                "read_only": True,
                "automatic_repair": False,
            }
        try:
            audit_state = _verify_restore_lock_recovery_audit_fd(
                descriptor, allow_unmatched_started=True
            )
        except RuntimeError as error:
            return {
                "status": "RECOVERY_AUDIT_INVALID",
                "audit_file": str(audit_file),
                "error": str(error)[:200],
                "read_only": True,
                "automatic_repair": False,
            }

        pending = audit_state["pending_recovery"]
        marker_state = "NOT_CHECKED"
        if pending is not None:
            lock_path = restore_lock_path()
            marker_fd = None
            try:
                marker_fd = os.open(
                    str(lock_path), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                )
                marker_meta = os.fstat(marker_fd)
                if not stat.S_ISREG(marker_meta.st_mode) or marker_meta.st_size > 65536:
                    marker_state = "UNREADABLE"
                else:
                    marker_bytes = os.read(marker_fd, 65537)
                    if len(marker_bytes) > 65536:
                        marker_state = "UNREADABLE"
                    elif hashlib.sha256(marker_bytes).hexdigest() == pending["marker_sha256"]:
                        marker_state = "MATCHES_INTERRUPTED_RECOVERY"
                    else:
                        marker_state = "DIFFERENT_MARKER_PRESENT"
            except FileNotFoundError:
                marker_state = "MARKER_ABSENT"
            except OSError:
                marker_state = "UNREADABLE"
            finally:
                if marker_fd is not None:
                    os.close(marker_fd)
        else:
            marker_state = "PRESENT_WITHOUT_PENDING_RECOVERY" if restore_lock_path().exists() else "NO_MARKER"

        status = "RECOVERY_INTERRUPTED_REQUIRES_OPERATOR_REVIEW" if pending else (
            "RECOVERY_AUDIT_EMPTY" if audit_state["record_count"] == 0 else "RECOVERY_AUDIT_VALID"
        )
        return {
            "status": status,
            "audit_file": str(audit_file),
            "record_count": audit_state["record_count"],
            "last_record_sha256": audit_state["last_record_sha256"],
            "pending_recovery": pending,
            "marker_state": marker_state,
            "read_only": True,
            "automatic_repair": False,
            "database_changed": False,
            "sidecars_changed": False,
        }
    finally:
        os.close(descriptor)


def recover_abandoned_restore_lock(
    operator_id: str,
    incident_ref: str,
    *,
    confirm: bool = False,
    audit_path: Path | None = None,
) -> dict:
    """Remove an abandoned restore marker only after explicit operator approval.

    This is an out-of-band recovery primitive, not an HTTP endpoint. It requires
    an operator identity, incident reference and explicit confirmation. It
    holds the process-shared exclusive guard while rechecking the exact marker,
    writes and fsyncs an audit STARTED event before unlinking, then records
    COMPLETE. It never changes the database or SQLite sidecars.
    """
    def valid_label(value: str, label: str) -> str:
        if (
            not isinstance(value, str)
            or not value.strip()
            or len(value.strip()) > 200
            or any(ord(char) < 32 or ord(char) == 127 for char in value)
        ):
            raise ValueError(f"A valid {label} is required.")
        return value.strip()

    operator = valid_label(operator_id, "operator identity")
    incident = valid_label(incident_ref, "incident reference")
    if confirm is not True:
        raise ValueError("Explicit confirm=True is required for lock recovery.")

    # The first read must establish that this is an abandoned marker, not an
    # active lock. A second identity check under the exclusive guard closes the
    # race between inspection and recovery.
    inspection = inspect_restore_lock()
    if inspection.get("status") != "ABANDONED_RESTORE_MARKER_REQUIRES_OPERATOR_REVIEW":
        raise RuntimeError("Restore marker is not confirmed abandoned; recovery refused.")

    lock_path = restore_lock_path()
    guard_path = database.restore_guard_path()
    marker_fd = None
    guard_fd = None
    try:
        marker_fd = os.open(
            str(lock_path), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        )
        marker_stat = os.fstat(marker_fd)
        if not __import__("stat").S_ISREG(marker_stat.st_mode):
            raise RuntimeError("Restore marker is not a regular file.")
        marker_identity = (
            marker_stat.st_dev, marker_stat.st_ino,
            marker_stat.st_size, marker_stat.st_mtime_ns,
        )
        marker_bytes = os.read(marker_fd, 65536)
        if os.read(marker_fd, 1):
            raise RuntimeError("Restore marker exceeds the recovery inspection limit.")

        guard_fd = os.open(
            str(guard_path),
            os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        if not __import__("stat").S_ISREG(os.fstat(guard_fd).st_mode):
            raise RuntimeError("Restore guard is not a regular file.")
        try:
            fcntl.flock(guard_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError("Restore guard is active; recovery refused.") from error

        current_stat = lock_path.lstat()
        current_identity = (
            current_stat.st_dev, current_stat.st_ino,
            current_stat.st_size, current_stat.st_mtime_ns,
        )
        os.lseek(marker_fd, 0, os.SEEK_SET)
        current_bytes = os.read(marker_fd, 65536)
        if (
            current_identity != marker_identity
            or current_bytes != marker_bytes
            or lock_path.is_symlink()
        ):
            raise RuntimeError("Restore marker changed during recovery; refused.")

        payload = json.loads(marker_bytes.decode("utf-8"))
        if (
            payload.get("plan_id") != inspection.get("plan_id")
            or payload.get("pid") != inspection.get("process_id")
            or payload.get("created_at") != inspection.get("created_at")
        ):
            raise RuntimeError("Restore marker metadata changed; recovery refused.")

        audit_file = Path(
            audit_path
            or (Path(database.DB_PATH).parent / "restore-lock-recovery.audit.jsonl")
        )
        audit_file.parent.mkdir(parents=True, exist_ok=True)
        audit_fd = os.open(
            str(audit_file),
            os.O_CREAT | os.O_APPEND | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            if not stat.S_ISREG(os.fstat(audit_fd).st_mode):
                raise RuntimeError("Recovery audit destination is not a regular file.")
            # Persist the audit filename itself before any recovery operation
            # can unlink the marker. This matters when the operator-selected
            # audit directory differs from the marker directory.
            audit_directory_fd = os.open(
                str(audit_file.parent),
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
            )
            try:
                os.fsync(audit_directory_fd)
            finally:
                os.close(audit_directory_fd)
            fcntl.flock(audit_fd, fcntl.LOCK_EX)
            audit_state = _verify_restore_lock_recovery_audit_fd(audit_fd)
            marker_hash = hashlib.sha256(marker_bytes).hexdigest()
            sequence = audit_state["record_count"]
            previous_hash = audit_state["last_record_sha256"]

            def write_audit(event: str) -> None:
                nonlocal sequence, previous_hash
                record = {
                    "sequence": sequence + 1,
                    "previous_record_sha256": previous_hash,
                    "event": event,
                    "timestamp": datetime.now(SAST).isoformat(),
                    "operator_id": operator,
                    "incident_ref": incident,
                    "plan_id": inspection["plan_id"],
                    "marker_sha256": marker_hash,
                    "marker_file": lock_path.name,
                    "database_changed": False,
                    "sidecars_changed": False,
                }
                record["record_sha256"] = _restore_lock_recovery_record_digest(record)
                data = (json.dumps(record, sort_keys=True) + "\n").encode("utf-8")
                view = memoryview(data)
                while view:
                    written = os.write(audit_fd, view)
                    if written <= 0:
                        raise OSError("Could not complete recovery audit write.")
                    view = view[written:]
                os.fsync(audit_fd)
                sequence += 1
                previous_hash = record["record_sha256"]

            write_audit("RESTORE_LOCK_RECOVERY_STARTED")
            lock_path.unlink()
            directory_fd = os.open(
                str(lock_path.parent), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            )
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            write_audit("RESTORE_LOCK_RECOVERY_COMPLETE")
        finally:
            os.close(audit_fd)
    finally:
        if guard_fd is not None:
            os.close(guard_fd)
        if marker_fd is not None:
            os.close(marker_fd)

    return {
        "status": "ABANDONED_RESTORE_LOCK_RECOVERED",
        "operator_id": operator,
        "incident_ref": incident,
        "audit_file": str(audit_file),
        "marker_removed": True,
        "database_changed": False,
        "sidecars_changed": False,
        "live_database_replaced": False,
        "trading_mode": "paper",
    }


def release_restore_lock(lock_token: str) -> dict:
    """Release an owned restore lock only after marker absence is durable.

    If a prior attempt unlinked this process's marker but could not sync the
    parent directory, a retry may finish the directory sync while the same
    in-process guard token is still registered. It never removes a replacement
    marker and never resumes from a token without its original guard fd.
    """
    if not isinstance(lock_token, str) or not re.fullmatch(r"[a-f0-9]{32}", lock_token):
        raise ValueError("A valid restore lock token is required.")
    guard_fd = _RESTORE_GUARD_FDS.get(lock_token)
    lock_path = restore_lock_path()
    marker_fd = None
    directory_fd = None
    marker_unlinked = False
    resumed_after_unlink = False
    try:
        try:
            marker_fd = os.open(
                str(lock_path), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            )
        except FileNotFoundError:
            # Only the process that still holds this exact token's guard may
            # complete a prior unlink whose directory fsync failed.
            if guard_fd is None:
                raise RuntimeError("Restore process-shared lock ownership is unavailable; marker remains active.")
            resumed_after_unlink = True
            marker_unlinked = True
        if marker_fd is not None:
            marker_stat = os.fstat(marker_fd)
            if not stat.S_ISREG(marker_stat.st_mode):
                raise RuntimeError("Restore lock marker is unsafe; lock remains active.")
            marker_bytes = os.read(marker_fd, 65536)
            if os.read(marker_fd, 1):
                raise RuntimeError("Restore lock marker exceeds the inspection limit; lock remains active.")
            try:
                payload = json.loads(
                    marker_bytes.decode("utf-8"),
                    object_pairs_hook=_restore_journal_object_without_duplicate_keys,
                    parse_constant=_reject_restore_journal_json_constant,
                )
            except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as error:
                raise RuntimeError("Restore lock metadata is unreadable; lock remains active.") from error
            if not isinstance(payload, dict) or payload.get("lock_token") != lock_token:
                raise RuntimeError("Restore lock ownership mismatch; lock remains active.")
            if guard_fd is None:
                raise RuntimeError("Restore process-shared lock ownership is unavailable; marker remains active.")

            current_stat = lock_path.lstat()
            if (
                stat.S_ISLNK(current_stat.st_mode)
                or (current_stat.st_dev, current_stat.st_ino) != (marker_stat.st_dev, marker_stat.st_ino)
            ):
                raise RuntimeError("Restore lock marker changed during release; lock remains active.")
            os.lseek(marker_fd, 0, os.SEEK_SET)
            if os.read(marker_fd, 65536) != marker_bytes or os.read(marker_fd, 1):
                raise RuntimeError("Restore lock marker changed during release; lock remains active.")

        directory_fd = os.open(
            str(lock_path.parent),
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        )
        if marker_fd is not None:
            os.unlink(lock_path)
            marker_unlinked = True
        os.fsync(directory_fd)
    except OSError as error:
        state = (
            "marker removal was not confirmed durable; process guard remains held."
            if marker_unlinked
            else "marker remains active and process guard remains held."
        )
        raise RuntimeError(f"Restore lock release could not be completed safely; {state}") from error
    finally:
        close_errors = []
        for descriptor in (marker_fd, directory_fd):
            if descriptor is None:
                continue
            try:
                os.close(descriptor)
            except OSError as error:
                close_errors.append(error)
        if close_errors:
            raise RuntimeError(
                "Restore lock release could not close its inspection descriptors safely; "
                "process guard remains held."
            ) from close_errors[0]

    try:
        os.close(guard_fd)
    except OSError as error:
        raise RuntimeError(
            "Restore lock marker is removed, but the process guard close failed; "
            "database access remains blocked while ownership is retained."
        ) from error
    _RESTORE_GUARD_FDS.pop(lock_token, None)
    return {
        "status": "RESTORE_LOCK_RELEASED",
        "database_quiescent": False,
        "marker_removed": True,
        "directory_synced": True,
        "resumed_after_unlink": resumed_after_unlink,
    }


def inspect_sqlite_sidecars(database_path: Path) -> dict:
    """Read-only inventory of SQLite WAL/SHM files beside a database.

    Any present sidecar requires explicit handling after the restore lock has
    quiesced the application. This helper never deletes, truncates, or opens
    sidecars as databases.
    """
    base = Path(database_path)
    found = []
    for suffix, kind in (("-wal", "WAL"), ("-shm", "SHM")):
        sidecar = Path(str(base) + suffix)
        try:
            metadata = sidecar.lstat()
        except FileNotFoundError:
            continue
        except OSError as error:
            raise ValueError(f"SQLite {kind} sidecar cannot be inspected safely.") from error
        if sidecar.is_symlink() or not sidecar.is_file():
            raise ValueError(f"SQLite {kind} sidecar is not a regular managed file.")
        found.append({"name": sidecar.name, "kind": kind, "size_bytes": metadata.st_size})
    return {
        "status": "SIDECARS_CLEAR" if not found else "SIDECARS_PRESENT_REQUIRES_LOCKED_HANDLING",
        "sidecars": found,
        "mutation_performed": False,
    }


def verify_restore_execution_readiness(
    source_backup: Path,
    staged_candidate: Path,
    rollback_backup: Path,
    *,
    backup_dir: Path | None = None,
    live_database_path: Path | None = None,
) -> dict:
    """Read-only final preflight for a restore and its rollback recovery point.

    This deliberately does not replace the live database or authorize execution.
    Every artifact must remain inside its designated managed directory and pass
    the full recovery inventory checks.
    """
    managed_backup_dir = Path(backup_dir or BACKUP_DIR).resolve()
    staging_dir = (managed_backup_dir / "recovery-staging").resolve()
    live_path = Path(live_database_path or database.DB_PATH).resolve()
    artifacts = (
        (Path(source_backup), managed_backup_dir, "source"),
        (Path(rollback_backup), managed_backup_dir, "rollback"),
        (Path(staged_candidate), staging_dir, "staged"),
    )
    for artifact, expected_parent, label in artifacts:
        if artifact.is_symlink() or not artifact.is_file():
            raise ValueError(f"The {label} recovery artifact is missing or unsafe.")
        if artifact.resolve().parent != expected_parent:
            raise ValueError(f"The {label} recovery artifact is outside its managed directory.")
        if artifact.resolve() == live_path:
            raise ValueError(f"The {label} recovery artifact cannot be the live database.")

    if live_path.is_symlink() or not live_path.is_file():
        raise ValueError("The live database is missing or unsafe.")

    source_inventory = inspect_recovery_candidates(managed_backup_dir)
    staged_inventory = inspect_recovery_candidates(staging_dir)
    if source_inventory.get("status") != "RECOVERY_INVENTORY_COMPLETE":
        raise ValueError("Managed backup inventory is unavailable.")
    if staged_inventory.get("status") != "RECOVERY_INVENTORY_COMPLETE":
        raise ValueError("Staged recovery inventory is unavailable.")

    source_records = {
        item.get("file_name"): item
        for item in source_inventory.get("candidates", [])
    }
    staged_records = {
        item.get("file_name"): item
        for item in staged_inventory.get("candidates", [])
    }
    if not source_records.get(Path(source_backup).name, {}).get("restore_ready"):
        raise ValueError("The selected source backup is not restore-ready.")
    if not source_records.get(Path(rollback_backup).name, {}).get("restore_ready"):
        raise ValueError("The rollback backup is not restore-ready.")
    if not staged_records.get(Path(staged_candidate).name, {}).get("restore_ready"):
        raise ValueError("The staged restore candidate is not restore-ready.")

    sidecar_inventory = inspect_sqlite_sidecars(live_path)
    if sidecar_inventory["status"] != "SIDECARS_CLEAR":
        raise ValueError(
            "The live database has WAL/SHM sidecars; stop external SQLite clients "
            "and complete operator-reviewed sidecar handling before preflight."
        )

    required_live_schema = {
        "users": {"user_id", "email", "password_hash", "created_at"},
        "paper_accounts": {"account_id", "starting_balance", "balance"},
        "trades": {
            "trade_id", "account_id", "client_order_id", "symbol", "side",
            "quantity", "entry_price", "stop_loss", "status", "risk_amount",
            "realized_pnl",
        },
        "position_groups": {"group_id", "account_id", "symbol", "side", "status"},
        "copy_relationships": {
            "relationship_id", "source_account_id", "follower_account_id", "status",
        },
        "copy_executions": {
            "copy_execution_id", "relationship_id", "source_trade_id", "status",
        },
        "participant_milestones": {"user_id", "joined_at", "first_trade_at"},
        "system_safety_state": {"singleton_id", "emergency_stop_active"},
    }
    try:
        # One explicit read transaction gives integrity, schema and FK checks
        # a consistent SQLite snapshot rather than opening the live DB twice.
        with sqlite3.connect(f"file:{live_path}?mode=ro", uri=True, timeout=10) as live:
            live.execute("BEGIN")
            live_integrity = live.execute("PRAGMA integrity_check").fetchone()
            if not live_integrity or live_integrity[0] != "ok":
                raise ValueError("The current live database failed its integrity check.")
            live_tables = {
                row[0] for row in live.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            missing_live_schema = []
            for table, required_columns in required_live_schema.items():
                if table not in live_tables:
                    missing_live_schema.append(table)
                    continue
                actual_columns = {
                    row[1] for row in live.execute(f"PRAGMA table_info('{table}')")
                }
                missing = required_columns - actual_columns
                if missing:
                    missing_live_schema.append(
                        f"{table}:" + ",".join(sorted(missing))
                    )
            live_fk_violations = live.execute("PRAGMA foreign_key_check").fetchall()
    except ValueError:
        raise
    except (sqlite3.Error, OSError) as error:
        raise ValueError("The current live database integrity/schema cannot be verified.") from error
    if missing_live_schema:
        raise ValueError(
            "The live database is missing required TaimoBot schema: "
            + "; ".join(missing_live_schema)
        )
    if live_fk_violations:
        raise ValueError("The live database has foreign-key violations.")


    return {
        "status": "RESTORE_EXECUTION_PREFLIGHT_PASSED",
        "source_backup_verified": True,
        "staged_candidate_verified": True,
        "rollback_backup_verified": True,
        "live_database_verified": True,
        "sqlite_sidecars": sidecar_inventory,
        "sidecar_handling_required_after_lock": sidecar_inventory["status"] != "SIDECARS_CLEAR",
        "execution_authorized": False,
        "live_database_replaced": False,
    }


def verify_restore_execution_readiness_under_lock(
    source_backup: Path,
    staged_candidate: Path,
    rollback_backup: Path,
    *,
    plan_id: str,
    lock_token: str,
    external_processes_stopped: bool = False,
    expected_artifact_hashes: dict | None = None,
    backup_dir: Path | None = None,
    live_database_path: Path | None = None,
) -> dict:
    """Repeat read-only restore checks while the matching app restore lock is held.

    The explicit external-process acknowledgement is an operator assertion, not
    something the application can independently prove. This function does not
    publish a database, remove sidecars, or authorize execution.
    """
    if not isinstance(plan_id, str) or not re.fullmatch(r"[a-f0-9]{32}", plan_id):
        raise ValueError("A valid restore plan ID is required.")
    if not isinstance(lock_token, str) or not re.fullmatch(r"[a-f0-9]{32}", lock_token):
        raise ValueError("A valid restore lock token is required.")
    if external_processes_stopped is not True:
        raise ValueError(
            "Operator must explicitly confirm all external SQLite processes are stopped."
        )
    lock_state = inspect_restore_lock()
    if (
        lock_state.get("status") != "RESTORE_LOCK_ACTIVE"
        or lock_state.get("plan_id") != plan_id
        or lock_state.get("lock_token") != lock_token
    ):
        raise RuntimeError("The matching active restore lock could not be verified.")

    required_hash_keys = {"source_sha256", "staged_sha256", "rollback_sha256"}
    if (
        not isinstance(expected_artifact_hashes, dict)
        or set(expected_artifact_hashes) != required_hash_keys
        or any(
            not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value)
            for value in expected_artifact_hashes.values()
        )
    ):
        raise ValueError("Expected source, staged and rollback SHA-256 fingerprints are required.")

    artifacts = (
        ("source_sha256", Path(source_backup)),
        ("staged_sha256", Path(staged_candidate)),
        ("rollback_sha256", Path(rollback_backup)),
    )
    before_hashes = {
        key: sha256_file(path)
        for key, path in artifacts
    }
    if before_hashes != expected_artifact_hashes:
        raise ValueError("Restore artifact fingerprint mismatch under lock.")
    result = verify_restore_execution_readiness(
        source_backup,
        staged_candidate,
        rollback_backup,
        backup_dir=backup_dir,
        live_database_path=live_database_path,
    )
    after_hashes = {
        key: sha256_file(path)
        for key, path in artifacts
    }
    if after_hashes != expected_artifact_hashes:
        raise ValueError("Restore artifact changed during locked preflight.")
    return {
        **result,
        "status": "RESTORE_EXECUTION_LOCKED_PREFLIGHT_PASSED",
        "restore_lock_verified": True,
        "artifact_fingerprints_match": True,
        "external_processes_stopped_operator_confirmed": True,
        "execution_authorized": False,
        "live_database_replaced": False,
    }


def verify_approved_restore_plan(
    plan,
    artifact_hashes,
    *,
    backup_dir: Path | None = None,
    live_database_path: Path | None = None,
    now: datetime | None = None,
) -> dict:
    """Read-only verification that an approved restore plan is still executable."""
    if plan is None or artifact_hashes is None:
        raise ValueError("Approved restore plan or artifact fingerprints are missing.")
    if plan["status"] != "APPROVED":
        raise ValueError("Restore plan must be APPROVED before execution preflight.")
    created_at = datetime.fromisoformat(plan["created_at"])
    if created_at.tzinfo is None:
        raise ValueError("Restore plan timestamp has no timezone.")
    current_time = now or datetime.now(SAST)
    if current_time.tzinfo is None:
        raise ValueError("Preflight clock must include a timezone.")
    age_seconds = (current_time.astimezone(SAST) - created_at.astimezone(SAST)).total_seconds()
    if age_seconds < 0 or age_seconds > 30 * 60:
        raise ValueError("Approved restore plan has expired; prepare a new plan.")
    if is_restore_lock_active():
        raise ValueError("A restore lock is already active; operator investigation is required.")

    managed_backup_dir = Path(backup_dir or BACKUP_DIR).resolve()
    staging_dir = (managed_backup_dir / "recovery-staging").resolve()
    selected_backup = Path(plan["backup_file"])
    staged_candidate = Path(plan["staging_file"])
    rollback_backup = Path(plan["pre_restore_backup_file"])
    for artifact, expected_parent, label in (
        (selected_backup, managed_backup_dir, "source"),
        (staged_candidate, staging_dir, "staged"),
        (rollback_backup, managed_backup_dir, "rollback"),
    ):
        if artifact.is_symlink() or not artifact.is_file() or artifact.resolve().parent != expected_parent:
            raise ValueError(f"The {label} restore artifact is missing or outside its managed directory.")

    current_hashes = (
        sha256_file(selected_backup),
        sha256_file(staged_candidate),
        sha256_file(rollback_backup),
    )
    expected_hashes = (
        artifact_hashes["source_sha256"],
        artifact_hashes["staged_sha256"],
        artifact_hashes["rollback_sha256"],
    )
    if current_hashes != expected_hashes:
        raise ValueError("Restore artifact fingerprint mismatch; prepare and approve a new plan.")

    readiness = verify_restore_execution_readiness(
        selected_backup,
        staged_candidate,
        rollback_backup,
        backup_dir=managed_backup_dir,
        live_database_path=live_database_path,
    )
    if readiness.get("status") != "RESTORE_EXECUTION_PREFLIGHT_PASSED":
        raise ValueError("Restore execution readiness checks did not pass.")
    return {
        "status": "APPROVED_RESTORE_PREFLIGHT_PASSED",
        "plan_id": plan["plan_id"],
        "plan_approved": True,
        "plan_unexpired": True,
        "artifact_fingerprints_match": True,
        "source_backup_verified": True,
        "staged_candidate_verified": True,
        "rollback_backup_verified": True,
        "live_database_verified": True,
        "sqlite_sidecars": readiness.get("sqlite_sidecars", {"status": "NOT_REPORTED", "sidecars": []}),
        "sidecar_handling_required_after_lock": readiness.get("sidecar_handling_required_after_lock", True),
        "execution_authorized": False,
        "live_database_replaced": False,
        "trading_mode": "paper",
    }
