"""Honest seven-layer health contract; no synthetic healthy states."""
from datetime import datetime, timezone


LAYER_IDS = (
    "MARKET",
    "ANALYSIS",
    "STRATEGY",
    "EXECUTION",
    "RISK",
    "LEARNING",
    "LEGACY",
)

HEALTH_STATUSES = (
    "HEALTHY",
    "DEGRADED",
    "OFFLINE",
    "BLOCKED",
    "NOT_IMPLEMENTED",
)

CONTRACT_VERSION = "layer-health-v1"


def build_layer_health_snapshot():
    """Return the current telemetry inventory without inventing layer heartbeats.

    Until a layer registers an authenticated health provider, its status remains
    NOT_IMPLEMENTED and its observed/last-success timestamps remain null.
    """
    generated_at = datetime.now(timezone.utc).isoformat()
    layers = [
        {
            "layer_id": layer_id,
            "status": "NOT_IMPLEMENTED",
            "observed_at": None,
            "last_success_at": None,
            "source_version": CONTRACT_VERSION,
            "dependencies": [],
            "capabilities": [],
            "limitations": [
                "No authenticated live health provider is registered for this layer."
            ],
            "evidence_ref": None,
        }
        for layer_id in LAYER_IDS
    ]
    return {
        "generated_at": generated_at,
        "overall_status": "NOT_IMPLEMENTED",
        "contract_version": CONTRACT_VERSION,
        "telemetry_registered": False,
        "layers": layers,
    }
