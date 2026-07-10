"""
Confirmation tokens for destructive tools (G11).

A destructive tool (activate/rollback/uninstall/cleanup/service_remove) is a
two-call handshake:

1. Called with no/invalid ``confirm``: the tool gathers a read-only PLAN and the
   current state of the *affected subtree*, mints an HMAC token binding
   (tool, normalized args, state hash, nonce, expiry), and returns the plan +
   token WITHOUT mutating anything.
2. Called again echoing the token: the server recomputes the HMAC over the same
   tool/args and the *freshly re-read* state, constant-time compares, checks the
   TTL and single-use nonce, and only then performs the mutation.

The token binds the exact args and the affected state: an ``activate 0.2.0``
token cannot authorize ``activate 0.1.5`` or ``uninstall 0.2.0``, and if the
relevant state changed between plan and confirm (TOCTOU), the hash differs and
the token is rejected. The secret is per-process, so a server restart voids all
outstanding tokens. The model can only relay a server-minted token; it cannot
forge one.
"""

import contextlib
import hashlib
import hmac
import json
import threading
from typing import Dict, Optional

from .errors import ConfirmationError


def _normalize_args(args: Dict) -> str:
    return json.dumps(args, sort_keys=True, separators=(",", ":"), default=str)


def state_hash(*parts: object) -> str:
    """A stable hash of the affected-subtree state (JSON-serializable parts)."""
    blob = json.dumps(parts, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(blob).hexdigest()


def _sign(secret: bytes, tool: str, args: Dict, state: str, nonce: str, exp: int) -> str:
    payload = "|".join([tool, _normalize_args(args), state, nonce, str(exp)]).encode()
    return hmac.new(secret, payload, hashlib.sha256).hexdigest()


def mint(secret: bytes, tool: str, args: Dict, state: str, nonce: str, expiry: int) -> str:
    """Create a confirmation token string ``<sig>.<nonce>.<exp>``."""
    sig = _sign(secret, tool, args, state, nonce, expiry)
    return f"{sig}.{nonce}.{expiry}"


def verify(
    secret: bytes,
    tool: str,
    args: Dict,
    state: str,
    presented: str,
    now: float,
    used_nonces: set,
    lock: Optional[threading.Lock] = None,
) -> None:
    """Validate a presented confirmation token, or raise ConfirmationError.

    On success the nonce is consumed (single use). The check-and-consume is done
    under ``lock`` so two concurrent confirmations of the same token cannot both
    succeed (FastMCP dispatches sync tools on a threadpool).
    """
    if not presented or not isinstance(presented, str):
        raise ConfirmationError(
            "this operation requires confirmation",
            operator_hint="call again with confirm=<token> from the returned plan",
        )
    try:
        sig, nonce, exp_str = presented.split(".")
        exp = int(exp_str)
    except (ValueError, AttributeError):
        raise ConfirmationError("malformed confirmation token")

    if now > exp:
        raise ConfirmationError(
            "confirmation token expired", operator_hint="request a fresh plan and retry"
        )

    # Signature + TTL are pure checks (no shared state); the nonce single-use
    # check must be atomic with its consumption.
    expected = _sign(secret, tool, args, state, nonce, exp)
    if not hmac.compare_digest(expected, sig):
        raise ConfirmationError(
            "confirmation token does not match this operation or the current NAS state",
            operator_hint="the target or state changed — request a fresh plan",
        )

    guard = lock if lock is not None else contextlib.nullcontext()
    with guard:
        if nonce in used_nonces:
            raise ConfirmationError("confirmation token was already used")
        used_nonces.add(nonce)
