"""Server-owned Market provider trust configuration.

This registry deliberately loads configuration from the process environment
only. It accepts no client-supplied trust assertions and never creates market
observations or provider heartbeats. A provider is not operationally registered
until a separately authenticated evidence adapter is implemented.
"""
import os


PROVIDER_ID_ENV = "TAIMOBOT_MARKET_PROVIDER_ID"
TRUST_ENV_FIELDS = {
    "source_verified": "TAIMOBOT_MARKET_SOURCE_VERIFIED",
    "symbol_mapping_verified": "TAIMOBOT_MARKET_SYMBOL_MAPPING_VERIFIED",
    "provenance_verified": "TAIMOBOT_MARKET_PROVENANCE_VERIFIED",
    "broker_specification_verified": "TAIMOBOT_MARKET_BROKER_SPECIFICATION_VERIFIED",
}


def _server_flag(environment, key):
    """Only an explicit server-side 'true' enables a trust flag."""
    return isinstance(environment.get(key), str) and environment[key].strip().lower() == "true"


def load_market_provider_configuration(environment=None):
    """Return a detached, non-secret view of server-controlled trust settings.

    Missing provider identity means no trusted provider is configured. Missing,
    malformed, or false verification flags always remain false. This function
    does not establish that a provider is reachable or that its evidence is
    authentic; those require a future authenticated adapter.
    """
    source = os.environ if environment is None else environment
    provider_value = source.get(PROVIDER_ID_ENV)
    provider_id = (
        provider_value.strip()
        if isinstance(provider_value, str) and provider_value.strip()
        else None
    )
    trusted_configuration = None
    if provider_id is not None:
        trusted_configuration = {
            "provider_id": provider_id,
            **{
                field: _server_flag(source, env_key)
                for field, env_key in TRUST_ENV_FIELDS.items()
            },
        }
    return {
        "provider_id": provider_id,
        "configuration_status": "CONFIGURED" if provider_id else "NOT_CONFIGURED",
        "trusted_configuration": trusted_configuration,
        "evidence_adapter_status": "NOT_IMPLEMENTED",
        "execution_eligible": False,
        "signals_generated": False,
        "orders_submitted": False,
    }
