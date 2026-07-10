"""Audit logging, including rejected/attacked calls (F9 / G16)."""

import json

from syrviscore_mcp.remote import RemoteRunner

from .conftest import make_config


def test_audit_event_records_rejection(tmp_path):
    audit = tmp_path / "audit.jsonl"
    runner = RemoteRunner(make_config(), audit_path=audit)
    runner.audit_event("service_remove", {"name": "../../etc"}, "ValidationError")

    lines = audit.read_text().strip().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["command"] == "service_remove"
    assert entry["rejected"] is True
    assert entry["outcome"] == "ValidationError"
    assert entry["args"] == {"name": "../../etc"}


def test_audit_never_raises_on_bad_path():
    # auditing must never block an operation, even if the path is unwritable
    runner = RemoteRunner(make_config(), audit_path=None)
    runner._audit_path = __import__("pathlib").Path("/proc/nonexistent/audit.jsonl")
    runner.audit_event("status", {}, "ok")  # should not raise
