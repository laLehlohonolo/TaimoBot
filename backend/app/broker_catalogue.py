"""Participant-facing broker catalogue; entries are candidates, not live integrations.

The catalogue intentionally advertises no working connection until a broker
adapter and its regional/legal-entity checks have passed acceptance.
"""

BROKER_CATALOGUE_VERSION = "broker-catalogue-v1"

_BROKERS = (
    {
        "broker_id": "exness",
        "display_name": "Exness",
        "platform_candidates": ["MT4", "MT5"],
        "connection_method": "MT5_READ_ONLY_BRIDGE",
        "regional_entity_review": "REQUIRED",
        "integration_status": "CANDIDATE_ONLY",
        "connection_status": "NOT_AVAILABLE",
        "supported_instruments": [],
        "execution_enabled": False,
    },
    {
        "broker_id": "avatrade",
        "display_name": "AvaTrade",
        "platform_candidates": ["MT4", "MT5", "AvaTrade proprietary platforms"],
        "connection_method": "MT5_READ_ONLY_BRIDGE",
        "regional_entity_review": "REQUIRED",
        "integration_status": "CANDIDATE_ONLY",
        "connection_status": "NOT_AVAILABLE",
        "supported_instruments": [],
        "execution_enabled": False,
    },
    {
        "broker_id": "vantage",
        "display_name": "Vantage",
        "platform_candidates": ["MT4", "MT5"],
        "connection_method": "MT5_READ_ONLY_BRIDGE",
        "regional_entity_review": "REQUIRED",
        "integration_status": "CANDIDATE_ONLY",
        "connection_status": "NOT_AVAILABLE",
        "supported_instruments": [],
        "execution_enabled": False,
    },
    {
        "broker_id": "fp_markets",
        "display_name": "FP Markets",
        "platform_candidates": ["MT4", "MT5", "cTrader (account availability to verify)"],
        "connection_method": "ADAPTER_REVIEW_REQUIRED",
        "regional_entity_review": "REQUIRED",
        "integration_status": "CANDIDATE_ONLY",
        "connection_status": "NOT_AVAILABLE",
        "supported_instruments": [],
        "execution_enabled": False,
    },
    {
        "broker_id": "tickmill",
        "display_name": "Tickmill",
        "platform_candidates": ["MT4", "MT5 (account availability to verify)"],
        "connection_method": "ADAPTER_REVIEW_REQUIRED",
        "regional_entity_review": "REQUIRED",
        "integration_status": "CANDIDATE_ONLY",
        "connection_status": "NOT_AVAILABLE",
        "supported_instruments": [],
        "execution_enabled": False,
    },
    {
        "broker_id": "hfm",
        "display_name": "HFM",
        "platform_candidates": ["MT4", "MT5 (account availability to verify)"],
        "connection_method": "ADAPTER_REVIEW_REQUIRED",
        "regional_entity_review": "REQUIRED",
        "integration_status": "CANDIDATE_ONLY",
        "connection_status": "NOT_AVAILABLE",
        "supported_instruments": [],
        "execution_enabled": False,
    },
)


def list_broker_catalogue():
    """Return detached catalogue records; never expose credentials or secrets."""
    return {
        "catalogue_version": BROKER_CATALOGUE_VERSION,
        "mode": "paper_trading",
        "read_only": True,
        "live_order_routing": False,
        "brokers": [dict(item, platform_candidates=list(item["platform_candidates"]),
                         supported_instruments=list(item["supported_instruments"]))
                    for item in _BROKERS],
    }
