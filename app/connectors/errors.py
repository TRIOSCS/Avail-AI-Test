"""Connector exception hierarchy.

Connectors raise these to signal hard failures that should:
  1. Flip ApiSource.status to 'error' via health_monitor.ping_source
  2. Bypass BaseConnector retry (these are not transient)
  3. Surface to the operator with a type-specific message

Called by: app/connectors/{sources,digikey,element14,mouser,oemsecrets,sourcengine}.py
Depends on: nothing (pure exception types)
"""


class ConnectorError(RuntimeError):
    """Base for connector hard failures.

    Subclass this for any condition that should flip ApiSource.status to 'error' and
    bypass BaseConnector's retry loop. Inheriting from RuntimeError keeps backward
    compatibility with `except RuntimeError` and `except Exception` catches in legacy
    code paths.
    """


class ConnectorAuthError(ConnectorError):
    """401/403 — bad/expired/revoked credentials, or 401-as-quota (e.g. OEMSecrets
    returns 401 for both bad-key and quota-exhausted).

    Operator action: rotate the API key in Admin > API Sources.
    """


class ConnectorRateLimitError(ConnectorError):
    """429 — rate limited, persistent across in-connector retries.

    Operator action: usually none; auto-recovers when the upstream's
    rate-limit window expires and the next health ping returns 200.
    Persistent rate-limiting from quota burn-down warrants a quota-plan
    upgrade — surfaced separately as ConnectorQuotaError.
    """


class ConnectorQuotaError(ConnectorError):
    """Explicit monthly/plan quota exhaustion (e.g. Nexar GraphQL 'You have exceeded
    your part limit').

    Operator action: upgrade plan or wait for monthly cycle reset.
    """
