"""Annotation-based MCP approval policy."""

from __future__ import annotations

import os
import sys
import threading

_SERVER_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "server")
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

from app.agent.mcp.approval import request_approval, requires_approval  # noqa: E402

from .mcp_fakes import FakeInteraction, make_server, make_tool  # noqa: E402


def test_auto_approval_only_for_destructive_tools():
    server = make_server(approval="auto")

    assert not requires_approval(server, make_tool(read_only=True))
    assert requires_approval(server, make_tool(destructive=True))
    assert requires_approval(server, make_tool())
    assert requires_approval(server, make_tool(read_only=True, destructive=True))


def test_always_and_never_override_annotations():
    assert requires_approval(make_server(approval="always"), make_tool(read_only=True))
    assert not requires_approval(make_server(approval="never"), make_tool(destructive=True))


def test_read_only_tool_skips_the_prompt():
    interaction = FakeInteraction()
    approved, reason = request_approval(
        interaction,
        make_server(),
        make_tool(read_only=True),
        {},
        cancel_event=None,
        timeout_seconds=5,
    )

    assert approved is True
    assert reason == ""
    assert interaction.asked == []


def test_destructive_tool_is_approved_when_the_user_allows():
    interaction = FakeInteraction(answer="Allow")
    approved, reason = request_approval(
        interaction,
        make_server(),
        make_tool(destructive=True),
        {"id": "1"},
        cancel_event=None,
        timeout_seconds=5,
    )

    assert approved is True
    assert reason == ""
    assert len(interaction.asked) == 1
    question = interaction.asked[0]["question"]
    assert "demo/get_item" in question
    assert '{"id": "1"}' in question


def test_deny_is_respected():
    approved, reason = request_approval(
        FakeInteraction(answer="Deny"),
        make_server(),
        make_tool(destructive=True),
        {},
        cancel_event=None,
        timeout_seconds=5,
    )

    assert approved is False
    assert "not approved" in reason


def test_missing_or_closed_interaction_fails_closed():
    server = make_server()
    info = make_tool(destructive=True)

    approved, reason = request_approval(None, server, info, {}, cancel_event=None, timeout_seconds=5)
    assert approved is False
    assert "no interactive approval channel" in reason

    approved, reason = request_approval(
        FakeInteraction(closed=True), server, info, {}, cancel_event=None, timeout_seconds=5
    )
    assert approved is False
    assert "no interactive approval channel" in reason


def test_timeout_or_transport_error_counts_as_denied():
    approved, reason = request_approval(
        FakeInteraction(error="timeout"),
        make_server(),
        make_tool(destructive=True),
        {},
        cancel_event=None,
        timeout_seconds=5,
    )

    assert approved is False
    assert "timeout" in reason


def test_cancelled_run_does_not_prompt():
    interaction = FakeInteraction()
    cancel_event = threading.Event()
    cancel_event.set()

    approved, reason = request_approval(
        interaction,
        make_server(),
        make_tool(destructive=True),
        {},
        cancel_event=cancel_event,
        timeout_seconds=5,
    )

    assert approved is False
    assert "cancelled" in reason
    assert interaction.asked == []
