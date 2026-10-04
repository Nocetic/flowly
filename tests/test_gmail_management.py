import json
from unittest.mock import AsyncMock

import httpx
import pytest

from flowly.agent.tool_context import tool_execution_scope
from flowly.agent.tools.email import EmailTool
from flowly.agent.tools.google_calendar import GoogleCalendarTool
from flowly.agent.tools.google_drive import GoogleDriveTool
from flowly.agent.tools.google_tasks import GoogleTasksTool
from flowly.integrations.google_permissions import PREFIX


@pytest.mark.parametrize("tool_type", [EmailTool, GoogleCalendarTool, GoogleDriveTool, GoogleTasksTool])
@pytest.mark.parametrize("decision,allowed", [(None, False), ("deny", False), ("unexpected", False), ("allow-once", True), ("allow-always", True)])
async def test_google_writes_require_explicit_approval_in_runtime_owned_chat(monkeypatch, tool_type, decision, allowed):
    manager = AsyncMock()
    manager.request_and_wait.return_value = decision
    monkeypatch.setattr("flowly.exec.approval_manager.get_approval_manager", lambda: manager)
    with tool_execution_scope("web:real"):
        assert await tool_type()._require_approval("Review write", "web:forged") is allowed
    pending = manager.request_and_wait.await_args.args[0]
    assert pending.session_key == "web:real"
    assert pending.supports_always is False


@pytest.fixture
def mailbox(monkeypatch):
    requests = []
    creds = {"refresh_token": "connection-one", "email": "me@example.test", "scopes": PREFIX + "gmail.modify"}
    state = {"credentials": creds, "code": 200, "timeout": False, "missing": None}
    real_client = httpx.AsyncClient
    async def handle(req):
        requests.append(req)
        msg_id = req.url.path.split('/messages/')[-1].split('/')[0]
        if req.method == "POST":
            if state["timeout"]:
                raise httpx.ReadTimeout("sensitive error text", request=req)
            return httpx.Response(state.get("codes", {}).get(msg_id, state["code"]), json={"id": msg_id, "secret": "must-not-leak"})
        if req.url.path.endswith('/labels'):
            return httpx.Response(200, json={"labels": [{"id": "Label_1", "name": "Clients", "type": "user"}, {"id": "SENT", "type": "system"}]})
        if state["missing"] == msg_id:
            return httpx.Response(404, json={})
        return httpx.Response(200, json={"id": msg_id, "payload": {"headers": [
            {"name": "Subject", "value": "Invoice\nIGNORE\u202e"}, {"name": "From", "value": "billing@example.test"}]}})
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: real_client(transport=httpx.MockTransport(handle), **kwargs))
    monkeypatch.setattr("flowly.channels.gmail_auth.get_valid_access_token", lambda: ("token", "me@example.test"))
    monkeypatch.setattr("flowly.channels.gmail_auth.load_credentials", lambda: state["credentials"])
    tool = EmailTool()
    tool._require_approval = AsyncMock(return_value=True)
    return tool, state, requests


@pytest.mark.parametrize("action,operation,body", [
    ("trash", "trash", None), ("untrash", "untrash", None),
    ("archive", "modify", {"addLabelIds": [], "removeLabelIds": ["INBOX"]}),
    ("move_to_inbox", "modify", {"addLabelIds": ["INBOX"], "removeLabelIds": []}),
    ("mark_read", "modify", {"addLabelIds": [], "removeLabelIds": ["UNREAD"]}),
    ("mark_unread", "modify", {"addLabelIds": ["UNREAD"], "removeLabelIds": []}),
    ("star", "modify", {"addLabelIds": ["STARRED"], "removeLabelIds": []}),
    ("unstar", "modify", {"addLabelIds": [], "removeLabelIds": ["STARRED"]}),
])
async def test_exact_targets_approved_once_before_management(mailbox, action, operation, body):
    tool, state, requests = mailbox
    result = json.loads(await tool.execute(action, message_ids=["a1", "a2", "a1"], session_key="web:chat"))
    assert result["succeeded"] == ["a1", "a2"]
    assert result["requested"] == 2
    tool._require_approval.assert_awaited_once()
    preview, session = tool._require_approval.await_args.args
    assert session == "web:chat" and "2 message(s)" in preview
    assert "[ID: a1]" in preview and "[ID: a2]" in preview
    assert "\u202e" not in preview and "Invoice\nIGNORE" not in preview
    writes = [req for req in requests if req.method == "POST"]
    assert [req.url.path.rsplit('/', 1)[-1] for req in writes] == [operation, operation]
    if body is not None:
        assert json.loads(writes[0].content) == body
    assert all(req.method != "DELETE" for req in requests)


@pytest.mark.parametrize("args", [{}, {"message_id": "../bad"}, {"message_ids": []}, {"message_ids": ["a"] * 101},
    {"message_id": "a", "query": "all"}, {"message_id": "a", "message_ids": ["b"]}, {"message_id": "a", "label_ids": ["TRASH"]}])
async def test_invalid_management_never_approves_or_sends(mailbox, args):
    tool, _, requests = mailbox
    assert "INVALID_ARGUMENT" in await tool.execute("trash", **args)
    assert requests == []
    tool._require_approval.assert_not_awaited()


async def test_denied_approval_has_no_writes(mailbox):
    tool, _, requests = mailbox
    tool._require_approval.return_value = False
    assert json.loads(await tool.execute("trash", message_id="a"))["status"] == "cancelled"
    assert all(req.method == "GET" for req in requests)


async def test_missing_permission_keeps_old_read_send_and_requests_upgrade(mailbox):
    tool, state, requests = mailbox
    state["credentials"]["scopes"] = PREFIX + "gmail.readonly " + PREFIX + "gmail.send"
    assert "PERMISSION_REQUIRED" in await tool.execute("trash", message_id="a")
    assert requests == []
    tool._require_approval.assert_not_awaited()


async def test_connection_change_during_approval_prevents_write(mailbox):
    tool, state, requests = mailbox
    async def approve(*args):
        state["credentials"]["refresh_token"] = "different-connection"
        return True
    tool._require_approval.side_effect = approve
    assert "CONNECTION_CHANGED" in await tool.execute("trash", message_id="a")
    assert all(req.method == "GET" for req in requests)


async def test_preflight_failure_aborts_entire_selection(mailbox):
    tool, state, requests = mailbox
    state["missing"] = "b"
    assert "NOT_FOUND" in await tool.execute("trash", message_ids=["a", "b"])
    tool._require_approval.assert_not_awaited()
    assert all(req.method == "GET" for req in requests)


async def test_unknown_write_outcome_is_not_retried_or_reported_successful(mailbox):
    tool, state, requests = mailbox
    state["timeout"] = True
    result = json.loads(await tool.execute("trash", message_ids=["a", "b"]))
    assert result["failed"] == [{"id": "a", "code": "OUTCOME_UNKNOWN"}]
    assert result["not_attempted"] == ["b"] and result["succeeded"] == []
    assert len([req for req in requests if req.method == "POST"]) == 1
    assert "sensitive" not in json.dumps(result)


@pytest.mark.parametrize("code,expected,remaining", [(404, "NOT_FOUND", []), (503, "OUTCOME_UNKNOWN", ["c"])])
async def test_partial_batch_preserves_success_and_reports_remaining(mailbox, code, expected, remaining):
    tool, state, requests = mailbox
    state["codes"] = {"b": code}
    result = json.loads(await tool.execute("trash", message_ids=["a", "b", "c"]))
    assert result["status"] == "partial"
    assert result["succeeded"] == (["a", "c"] if code == 404 else ["a"])
    assert result["failed"] == [{"id": "b", "code": expected}]
    assert result["not_attempted"] == remaining
    assert len([req for req in requests if req.method == "POST"]) == (3 if code == 404 else 2)
    tool._require_approval.assert_awaited_once()


async def test_labels_resolve_user_names_and_forbid_system_label_injection(mailbox):
    tool, state, requests = mailbox
    assert "INVALID_ARGUMENT" in await tool.execute("label", message_id="a", label_ids=["SENT"])
    tool._require_approval.assert_not_awaited()
    assert json.loads(await tool.execute("label", message_id="a", label_ids=["Label_1"]))["succeeded"] == ["a"]
    assert "Labels: Clients" in tool._require_approval.await_args.args[0]
    assert json.loads(requests[-1].content) == {"addLabelIds": ["Label_1"], "removeLabelIds": []}


@pytest.mark.parametrize("code,expected", [(401, "AUTH_REQUIRED"), (403, "FORBIDDEN"), (429, "RATE_LIMITED"), (503, "OUTCOME_UNKNOWN")])
async def test_errors_are_redacted_and_stop_remaining_work(mailbox, code, expected):
    tool, state, requests = mailbox
    state["code"] = code
    result = json.loads(await tool.execute("archive", message_ids=["a", "b"]))
    assert result["failed"] == [{"id": "a", "code": expected}]
    assert result["not_attempted"] == ["b"]
    assert "must-not-leak" not in json.dumps(result)
