"""Vocabulary for how a routed service is reached from outside SyrvisCore.

Every service SyrvisCore routes through Traefik is reachable on one of two planes:

- ``internal`` (default) — LAN-only. Traefik routes it with a DNS-01 certificate;
  the *only* external step is a local DNS record pointing the hostname at Traefik.
- ``tunnel`` — reachable remotely through the Cloudflare Tunnel, gated by
  Cloudflare Access.

This is **declared intent**, not an action SyrvisCore performs: SyrvisCore always
routes the service the same way at the Traefik layer. What differs is the external
state a deployment must create, which ``syrvis stack hostnames`` reports:

- ``internal`` -> a LAN DNS A record ``<host> -> TRAEFIK_IP``.
- ``tunnel``   -> a Cloudflare Tunnel public hostname + an Access policy
  (plus a proxied CNAME to the tunnel).

Keeping exposure a declaration keeps SyrvisCore generic: it never needs Cloudflare
API access. A config repo (e.g. home-tech) reads the report and reconciles the
records via its own MCP tooling.

Kept import-light and Python 3.8-clean — imported by the on-NAS CLI, the L2
service schema, and the hostnames report.
"""

INTERNAL = "internal"
TUNNEL = "tunnel"

# The full, ordered set of valid exposures.
EXPOSURES = (INTERNAL, TUNNEL)

# The posture a service gets when it declares nothing: LAN-only.
DEFAULT = INTERNAL


def is_valid(value) -> bool:
    """Return True if ``value`` is a known exposure."""
    return value in EXPOSURES


def normalize(value, default: str = DEFAULT) -> str:
    """Normalize a user-supplied exposure to a canonical value.

    ``None`` / empty -> ``default``. Otherwise lower-cased and validated.

    Raises:
        ValueError: if ``value`` is a non-empty string that isn't a known exposure.
    """
    if value is None or value == "":
        return default
    normalized = str(value).strip().lower()
    if normalized not in EXPOSURES:
        raise ValueError(
            "invalid exposure {!r}: must be one of {}".format(value, ", ".join(EXPOSURES))
        )
    return normalized
