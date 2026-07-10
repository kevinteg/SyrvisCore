"""Confirmation-token handshake (G11)."""

import pytest

from syrviscore_mcp import tokens
from syrviscore_mcp.errors import ConfirmationError

SECRET = b"test-secret"


def test_mint_verify_roundtrip():
    state = tokens.state_hash({"active": "0.1.0"})
    tok = tokens.mint(SECRET, "activate", {"version": "0.2.0"}, state, "nonce1", 1000)
    used = set()
    tokens.verify(SECRET, "activate", {"version": "0.2.0"}, state, tok, now=500, used_nonces=used)
    assert "nonce1" in used


def test_no_token_required():
    with pytest.raises(ConfirmationError):
        tokens.verify(SECRET, "activate", {"version": "0.2.0"}, "s", "", now=1, used_nonces=set())


def test_wrong_args_rejected():
    state = tokens.state_hash({"active": "0.1.0"})
    tok = tokens.mint(SECRET, "activate", {"version": "0.2.0"}, state, "n", 1000)
    with pytest.raises(ConfirmationError):
        # token minted for 0.2.0 cannot authorize 0.1.5
        tokens.verify(SECRET, "activate", {"version": "0.1.5"}, state, tok, 1, set())


def test_wrong_tool_rejected():
    state = "s"
    tok = tokens.mint(SECRET, "activate", {"version": "0.2.0"}, state, "n", 1000)
    with pytest.raises(ConfirmationError):
        tokens.verify(SECRET, "uninstall", {"version": "0.2.0"}, state, tok, 1, set())


def test_state_drift_rejected():
    tok = tokens.mint(SECRET, "activate", {"version": "0.2.0"}, "state-A", "n", 1000)
    with pytest.raises(ConfirmationError):
        # state changed between plan and confirm -> hash differs -> rejected
        tokens.verify(SECRET, "activate", {"version": "0.2.0"}, "state-B", tok, 1, set())


def test_expired_rejected():
    tok = tokens.mint(SECRET, "activate", {"version": "0.2.0"}, "s", "n", 100)
    with pytest.raises(ConfirmationError):
        tokens.verify(
            SECRET, "activate", {"version": "0.2.0"}, "s", tok, now=200, used_nonces=set()
        )


def test_replay_rejected():
    tok = tokens.mint(SECRET, "activate", {"version": "0.2.0"}, "s", "n", 1000)
    used = set()
    tokens.verify(SECRET, "activate", {"version": "0.2.0"}, "s", tok, 1, used)
    with pytest.raises(ConfirmationError):
        tokens.verify(SECRET, "activate", {"version": "0.2.0"}, "s", tok, 1, used)


def test_malformed_token():
    with pytest.raises(ConfirmationError):
        tokens.verify(SECRET, "activate", {}, "s", "garbage", 1, set())


def test_wrong_secret_rejected():
    tok = tokens.mint(SECRET, "activate", {"version": "0.2.0"}, "s", "n", 1000)
    with pytest.raises(ConfirmationError):
        tokens.verify(b"other-secret", "activate", {"version": "0.2.0"}, "s", tok, 1, set())


def test_concurrent_confirm_only_one_succeeds():
    """F7: two threads confirming the same token — exactly one wins (locked)."""
    import threading

    tok = tokens.mint(SECRET, "uninstall", {"version": "0.1.0"}, "s", "n", 10_000)
    used = set()
    lock = threading.Lock()
    barrier = threading.Barrier(2)
    results = []

    def worker():
        barrier.wait()  # maximize interleaving
        try:
            tokens.verify(SECRET, "uninstall", {"version": "0.1.0"}, "s", tok, 1, used, lock=lock)
            results.append("ok")
        except ConfirmationError:
            results.append("rejected")

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(results) == ["ok", "rejected"]
