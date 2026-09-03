"""Tests for deterministic, typed tool fault injection."""

import pytest

from agent_reliability_lab.tools.faults import (
    FaultKind,
    FaultRule,
    InjectedFault,
    no_faults,
    timeout_on_attempt,
)


def test_timeout_rule_matches_only_its_named_tool_and_attempt() -> None:
    """A fault matcher that ignores the tool name or attempt must fail."""
    rule = timeout_on_attempt(2, tool_name="search_recent_logs")

    assert rule.fault_for("search_recent_logs", 1) is None
    assert rule.fault_for("get_service_health", 2) is None
    fault = rule.fault_for("search_recent_logs", 2)
    assert fault is not None
    assert fault.kind is FaultKind.TIMEOUT
    assert fault.code == "tool_timeout"
    assert fault.transient is True


def test_rules_are_schema_validated_and_no_faults_never_injects() -> None:
    """Untyped or invalid fault definitions must not silently enter a run."""
    with pytest.raises(ValueError):
        FaultRule(tool_name="", attempt=1, kind="timeout")

    assert no_faults().fault_for("search_recent_logs", 1) is None


def test_injected_fault_exposes_a_typed_transient_error() -> None:
    """Collapsing deterministic injected faults to a generic exception must fail."""
    fault = InjectedFault(FaultKind.TIMEOUT, tool_name="search_recent_logs")

    assert fault.code == "tool_timeout"
    assert fault.transient is True
