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


def build_layer_health_snapshot(market_health=None):
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
    if market_health is not None:
        market_layer = next(item for item in layers if item["layer_id"] == "MARKET")
        market_layer.update({
            "status": market_health.get("status", "NOT_IMPLEMENTED"),
            "observed_at": market_health.get("observed_at"),
            "last_success_at": market_health.get("last_success_at"),
            "source_version": market_health.get("contract_version", CONTRACT_VERSION),
            "capabilities": ["provider_evidence_evaluation"],
            "limitations": [market_health.get("reason", "No Market provider is registered.")],
            "evidence_ref": market_health.get("evidence_id"),
            "execution_eligible": False,
            "signals_generated": False,
            "orders_submitted": False,
        })
    return {
        "generated_at": generated_at,
        "overall_status": "NOT_IMPLEMENTED",
        "contract_version": CONTRACT_VERSION,
        "telemetry_registered": False,
        "layers": layers,
    }
