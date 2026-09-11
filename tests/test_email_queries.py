"""Gmail read contract: synthetic HTTP only, no credentials or real mailbox."""

import asyncio
import base64
import json
from unittest.mock import AsyncMock

import httpx
import pytest

from flowly.agent.tools.email import EmailTool


def encoded(text, encoding="utf-8"):
    return base64.urlsafe_b64encode(text.encode(encoding)).decode().rstrip("=")


def message(msg_id="a1", **overrides):
    return {
        "id": msg_id,
        "threadId": "thread1",
        "internalDate": "1789120800000",
        "labelIds": ["INBOX", "UNREAD"],
        "snippet": "A preview",
        "payload": {
            "mimeType": "text/plain",
            "headers": [
                {"name": "From", "value": "Sender <sender@example.test>"},
                {"name": "To", "value": "owner@example.test"},
                {"name": "Subject", "value": "Test subject"},
                {"name": "Date", "value": "Fri, 11 Sep 2026 12:00:00 +0300"},
            ],
            "body": {"data": encoded("Hello")},
        },
        **overrides,
    }


@pytest.fixture
def mailbox(monkeypatch):
    requests = []
    state = {"handler": lambda request: httpx.Response(200, json=message())}
    real_client = httpx.AsyncClient

    async def handler(request):
        requests.append(request)
        result = state["handler"](request)
        return await result if asyncio.iscoroutine(result) else result

    monkeypatch.setattr(
        "flowly.agent.tools.email.httpx.AsyncClient",
        lambda **kwargs: real_client(
            transport=httpx.MockTransport(handler),
            **kwargs,
        ),
    )
    monkeypatch.setattr(
        "flowly.channels.gmail_auth.get_valid_access_token",
        lambda **kwargs: (
            "synthetic-token",
            "owner@example.test",
        ),
    )
    return state, requests


@pytest.mark.asyncio
async def test_headers_are_repeated_parameters_and_count_is_not_capped_at_ten(mailbox):
    state, requests = mailbox

    def handler(request):
        if request.url.path.endswith("/messages"):
            return httpx.Response(
                200,
                json={
                    "messages": [{"id": f"a{i}"} for i in range(25)],
                    "nextPageToken": "next-page",
                    "resultSizeEstimate": 80,
                },
            )
        assert request.url.params.get_list("metadataHeaders") == ["From", "To", "Subject", "Date"]
        return httpx.Response(200, json=message(request.url.path.split("/")[-1]))

    state["handler"] = handler
    result = json.loads(await EmailTool().execute("search", query="is:unread", max_results=25))
    assert requests[0].url.params["maxResults"] == "25"
    assert result["returned_count"] == 25
    assert result["next_page_token"] == "next-page"
    assert result["has_more"] is True
    assert result["total_estimate"] == 80
    assert result["messages"][0]["subject"] == "Test subject"


@pytest.mark.asyncio
async def test_inbox_preserves_filter_and_page_cursor(mailbox):
    state, requests = mailbox
    state["handler"] = lambda request: httpx.Response(200, json={"messages": []})
    result = json.loads(
        await EmailTool().execute(
            "inbox",
            query="from:a@example.test OR from:b@example.test",
            page_token="page+2=",
        )
    )
    assert requests[0].url.params["q"] == "from:a@example.test OR from:b@example.test"
    assert requests[0].url.params.get_list("labelIds") == ["INBOX"]
    assert requests[0].url.params["pageToken"] == "page+2="
    assert result["status"] == "ok" and result["has_more"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_results": 0},
        {"max_results": -1},
        {"max_results": 101},
        {"max_results": True},
        {"max_results": "25"},
        {"max_results": None},
        {"page_token": 3},
        {"include_spam_trash": "false"},
    ],
)
async def test_invalid_arguments_fail_before_http(mailbox, kwargs):
    _, requests = mailbox
    assert (await EmailTool().execute("inbox", **kwargs)).startswith("Error: INVALID_ARGUMENT")
    assert not requests


@pytest.mark.asyncio
async def test_failed_details_are_not_reported_as_zero_matches(mailbox):
    state, _ = mailbox
    state["handler"] = (
        lambda request: httpx.Response(200, json={"messages": [{"id": "a1"}]})
        if request.url.path.endswith("/messages")
        else httpx.Response(403, json={"error": {"message": "private-data"}})
    )
    result = json.loads(await EmailTool().execute("search", query="invoice"))
    assert result["status"] == "partial"
    assert result["listed_count"] == 1 and result["returned_count"] == 0
    assert result["errors"][0]["id"] == "a1"
    assert result["errors"][0]["code"] == "FORBIDDEN"
    assert "private-data" not in json.dumps(result)


@pytest.mark.asyncio
async def test_root_html_and_charset_are_read_without_fetching_external_resources(mailbox):
    state, requests = mailbox
    state["handler"] = lambda request: httpx.Response(
        200,
        json=message(
            payload={
                "mimeType": "text/html",
                "headers": [{"name": "Content-Type", "value": "text/html; charset=iso-8859-9"}],
                "body": {
                    "data": encoded(
                        '<style>private-style</style><p>İyi &amp; güzel</p><script>private-script</script><img src="https://remote.test/x">',
                        "iso-8859-9",
                    )
                },
            }
        ),
    )
    result = json.loads(await EmailTool().execute("read", message_id="a1"))
    assert result["body"] == "İyi & güzel"
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_separately_stored_text_body_is_loaded_but_file_attachment_is_not(mailbox):
    state, requests = mailbox
    state["handler"] = (
        lambda request: httpx.Response(200, json={"data": encoded("Stored body")})
        if "/attachments/" in request.url.path
        else httpx.Response(
            200,
            json=message(
                payload={
                    "mimeType": "multipart/mixed",
                    "parts": [
                        {"mimeType": "text/plain", "body": {"attachmentId": "body1", "size": 11}},
                        {
                            "mimeType": "text/plain",
                            "filename": "private.txt",
                            "body": {"attachmentId": "file1", "size": 10},
                        },
                    ],
                }
            ),
        )
    )
    result = json.loads(await EmailTool().execute("read", message_id="a1"))
    assert result["body"] == "Stored body"
    assert result["attachments"][0]["filename"] == "private.txt"
    assert [request.url.path for request in requests][-1].endswith("/attachments/body1")
    assert len(requests) == 2


@pytest.mark.asyncio
async def test_body_pagination_preserves_full_text_without_silent_truncation(mailbox):
    state, _ = mailbox
    state["handler"] = lambda request: httpx.Response(
        200,
        json=message(
            payload={
                "mimeType": "text/plain",
                "body": {"data": encoded("abcdefghij")},
            }
        ),
    )
    first = json.loads(await EmailTool().execute("read", message_id="a1", body_limit=4))
    second = json.loads(
        await EmailTool().execute(
            "read", message_id="a1", body_limit=10, body_offset=first["next_body_offset"]
        )
    )
    assert first["body"] == "abcd" and first["body_truncated"] is True
    assert first["body"] + second["body"] == "abcdefghij"
    assert second["next_body_offset"] is None


@pytest.mark.asyncio
async def test_invalid_message_path_never_reaches_api(mailbox):
    _, requests = mailbox
    assert (await EmailTool().execute("read", message_id="../profile?x=y")).startswith(
        "Error: INVALID_ARGUMENT"
    )
    assert not requests


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,reason", [(429, None), (503, None), (403, "userRateLimitExceeded")]
)
async def test_transient_read_errors_retry_then_succeed(mailbox, monkeypatch, status, reason):
    from flowly.integrations import gmail_reader

    sleep = AsyncMock()
    monkeypatch.setattr(gmail_reader.asyncio, "sleep", sleep)
    state, requests = mailbox

    def handler(request):
        if len(requests) < 3:
            return httpx.Response(status, json={"error": {"errors": [{"reason": reason}]}})
        return httpx.Response(200, json=message())

    state["handler"] = handler
    result = json.loads(await EmailTool().execute("read", message_id="a1"))
    assert result["body"] == "Hello"
    assert len(requests) == 3 and sleep.await_count == 2


@pytest.mark.asyncio
async def test_network_failure_is_bounded_and_redacted(mailbox, monkeypatch):
    from flowly.integrations import gmail_reader

    monkeypatch.setattr(gmail_reader.asyncio, "sleep", AsyncMock())
    state, requests = mailbox

    def handler(request):
        raise httpx.ConnectError("private credential in diagnostic", request=request)

    state["handler"] = handler
    result = await EmailTool().execute("read", message_id="a1")
    assert result.startswith("Error: UNAVAILABLE")
    assert "private" not in result and len(requests) == 3


@pytest.mark.asyncio
async def test_long_retry_after_does_not_hammer_google(mailbox, monkeypatch):
    from flowly.integrations import gmail_reader

    sleep = AsyncMock()
    monkeypatch.setattr(gmail_reader.asyncio, "sleep", sleep)
    state, requests = mailbox
    state["handler"] = lambda request: httpx.Response(429, json={}, headers={"Retry-After": "120"})
    assert (await EmailTool().execute("read", message_id="a1")).startswith("Error: RATE_LIMITED")
    assert len(requests) == 1 and sleep.await_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,code",
    [(400, "INVALID_REQUEST"), (401, "AUTH_REQUIRED"), (403, "FORBIDDEN"), (404, "NOT_FOUND")],
)
async def test_permanent_errors_are_distinct_and_not_retried(mailbox, status, code):
    state, requests = mailbox
    state["handler"] = lambda request: httpx.Response(
        status, json={"error": {"message": "private content"}}
    )
    result = await EmailTool().execute("read", message_id="a1")
    assert result.startswith(f"Error: {code}")
    assert "private content" not in result and len(requests) == 1


@pytest.mark.asyncio
async def test_details_use_bounded_concurrency_and_preserve_order(mailbox):
    state, _ = mailbox
    active, peak = 0, 0

    async def handler(request):
        nonlocal active, peak
        if request.url.path.endswith("/messages"):
            return httpx.Response(200, json={"messages": [{"id": f"a{i}"} for i in range(25)]})
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.001)
        active -= 1
        return httpx.Response(200, json=message(request.url.path.split("/")[-1]))

    state["handler"] = handler
    result = json.loads(await EmailTool().execute("inbox", max_results=25))
    assert 1 < peak <= 5
    assert [item["id"] for item in result["messages"]] == [f"a{i}" for i in range(25)]


@pytest.mark.asyncio
async def test_partial_failure_keeps_successful_messages_and_cursor(mailbox):
    state, _ = mailbox

    def handler(request):
        if request.url.path.endswith("/messages"):
            return httpx.Response(
                200, json={"messages": [{"id": "a1"}, {"id": "a2"}], "nextPageToken": "next"}
            )
        if request.url.path.endswith("/a2"):
            return httpx.Response(404, json={})
        return httpx.Response(200, json=message())

    state["handler"] = handler
    result = json.loads(await EmailTool().execute("inbox"))
    assert result["status"] == "partial" and result["returned_count"] == 1
    assert result["messages"][0]["id"] == "a1"
    assert result["next_page_token"] == "next"
    assert result["errors"][0]["id"] == "a2"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "data",
    [
        [],
        {"messages": [None]},
        {"messages": [{"id": "../x"}]},
        {"messages": None},
        {"nextPageToken": 32},
    ],
)
async def test_malformed_pages_are_not_reported_as_empty(mailbox, data):
    state, _ = mailbox
    state["handler"] = lambda request: httpx.Response(200, json=data)
    assert (await EmailTool().execute("search", query="invoice")).startswith(
        "Error: INVALID_RESPONSE"
    )


@pytest.mark.asyncio
async def test_nested_alternative_prefers_plain_and_keeps_other_inline_text(mailbox):
    state, _ = mailbox
    state["handler"] = lambda request: httpx.Response(
        200,
        json=message(
            payload={
                "mimeType": "multipart/mixed",
                "parts": [
                    {
                        "mimeType": "multipart/alternative",
                        "parts": [
                            {
                                "mimeType": "text/html",
                                "body": {"data": encoded("<p>Duplicate HTML</p>")},
                            },
                            {"mimeType": "text/plain", "body": {"data": encoded("Plain text")}},
                        ],
                    },
                    {"mimeType": "text/plain", "body": {"data": encoded("Inline footer")}},
                ],
            }
        ),
    )
    result = json.loads(await EmailTool().execute("read", message_id="a1"))
    assert result["body"] == "Plain text\n\nInline footer"


@pytest.mark.asyncio
async def test_missing_headers_are_explicit_with_received_date_fallback(mailbox):
    state, _ = mailbox
    state["handler"] = lambda request: httpx.Response(
        200, json=message(payload={"mimeType": "text/plain", "headers": [], "body": {}})
    )
    result = json.loads(await EmailTool().execute("read", message_id="a1"))
    assert result["subject"] is None and result["from"] is None
    assert result["date"] == result["received_at"] and result["received_at"] is not None
    assert result["missing_headers"] == ["from", "subject", "date"]
    assert result["warnings"]


@pytest.mark.asyncio
async def test_encoded_headers_are_decoded(mailbox):
    state, _ = mailbox
    state["handler"] = lambda request: httpx.Response(
        200,
        json=message(
            payload={
                "mimeType": "text/plain",
                "body": {},
                "headers": [{"name": "sUbJeCt", "value": "=?utf-8?b?xLB5aQ==?="}],
            }
        ),
    )
    result = json.loads(await EmailTool().execute("read", message_id="a1"))
    assert result["subject"] == "İyi"


@pytest.mark.asyncio
async def test_query_syntax_and_spam_scope_are_preserved(mailbox):
    state, requests = mailbox
    state["handler"] = lambda request: httpx.Response(200, json={})
    query = 'subject:"monthly bill" (from:a@example.test OR from:b@example.test) after:1789084800 -label:work'
    await EmailTool().execute("search", query=query, include_spam_trash=True)
    assert requests[0].url.params["q"] == query
    assert requests[0].url.params["includeSpamTrash"] == "true"
    assert "labelIds" not in requests[0].url.params


@pytest.mark.asyncio
async def test_read_cancellation_is_not_swallowed(mailbox):
    state, _ = mailbox

    def handler(request):
        raise asyncio.CancelledError()

    state["handler"] = handler
    with pytest.raises(asyncio.CancelledError):
        await EmailTool().execute("read", message_id="a1")


@pytest.mark.asyncio
async def test_invalid_base64_is_not_reported_as_empty_body(mailbox):
    state, _ = mailbox
    state["handler"] = lambda request: httpx.Response(
        200, json=message(payload={"mimeType": "text/plain", "body": {"data": "not base64!"}})
    )
    assert (await EmailTool().execute("read", message_id="a1")).startswith(
        "Error: INVALID_RESPONSE"
    )


@pytest.mark.asyncio
async def test_reply_uses_correct_headers_and_never_retries_send(mailbox, monkeypatch):
    state, requests = mailbox
    monkeypatch.setattr(EmailTool, "_require_approval", AsyncMock(return_value=True))
    monkeypatch.setattr(EmailTool, "_build_mime_message", lambda *args, **kwargs: "synthetic-mime")

    def handler(request):
        if request.method == "GET":
            assert request.url.params.get_list("metadataHeaders") == [
                "From",
                "Subject",
                "Message-ID",
            ]
            return httpx.Response(200, json=message())
        return httpx.Response(503, json={})

    state["handler"] = handler
    result = await EmailTool().execute("reply", message_id="a1", body="Thanks")
    assert result.startswith("Error sending reply")
    assert sum(request.method == "POST" for request in requests) == 1


@pytest.mark.asyncio
async def test_denied_reply_performs_no_mailbox_requests(mailbox, monkeypatch):
    _, requests = mailbox
    monkeypatch.setattr(EmailTool, "_require_approval", AsyncMock(return_value=False))
    assert "cancelled" in await EmailTool().execute("reply", message_id="a1", body="Thanks")
    assert not requests


@pytest.mark.asyncio
async def test_large_page_survives_agent_result_sanitization(mailbox, monkeypatch):
    from flowly.agent import loop

    state, _ = mailbox

    def handler(request):
        if request.url.path.endswith("/messages"):
            return httpx.Response(
                200,
                json={"messages": [{"id": f"a{i}"} for i in range(100)], "nextPageToken": "next"},
            )
        return httpx.Response(200, json=message(request.url.path.split("/")[-1]))

    state["handler"] = handler

    def forbidden_spill(*args):
        pytest.fail("Ordinary Gmail pages must not need a temporary result file")

    monkeypatch.setattr(loop, "spill_tool_result", forbidden_spill)
    result = await EmailTool().execute("inbox", max_results=100)
    assert len(result) > 8000
    parsed = json.loads(loop._sanitize_tool_result(result, "email"))
    assert parsed["returned_count"] == 100 and parsed["next_page_token"] == "next"


@pytest.mark.asyncio
async def test_two_pages_are_distinct_and_do_not_restart_the_search(mailbox):
    state, requests = mailbox

    def handler(request):
        if request.url.path.endswith("/messages"):
            if request.url.params.get("pageToken") == "next":
                return httpx.Response(200, json={"messages": [{"id": "b1"}]})
            return httpx.Response(200, json={"messages": [{"id": "a1"}], "nextPageToken": "next"})
        return httpx.Response(200, json=message(request.url.path.split("/")[-1]))

    state["handler"] = handler
    first = json.loads(await EmailTool().execute("search", query="subject:invoice"))
    second = json.loads(
        await EmailTool().execute(
            "search", query="subject:invoice", page_token=first["next_page_token"]
        )
    )
    assert first["messages"][0]["id"] == "a1" and second["messages"][0]["id"] == "b1"
    assert second["has_more"] is False
    assert [
        request.url.params.get("q")
        for request in requests
        if request.url.path.endswith("/messages")
    ] == ["subject:invoice"] * 2


@pytest.mark.asyncio
async def test_oversize_response_is_bounded(mailbox, monkeypatch):
    from flowly.integrations import gmail_reader

    monkeypatch.setattr(gmail_reader, "MAX_RESPONSE_BYTES", 100)
    state, requests = mailbox
    state["handler"] = lambda request: httpx.Response(200, content=b"x" * 101)
    assert (await EmailTool().execute("read", message_id="a1")).startswith(
        "Error: RESPONSE_TOO_LARGE"
    )
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_oversize_separate_body_is_not_downloaded(mailbox, monkeypatch):
    from flowly.integrations import gmail_reader

    monkeypatch.setattr(gmail_reader, "MAX_BODY_BYTES", 100)
    state, requests = mailbox
    state["handler"] = lambda request: httpx.Response(
        200,
        json=message(
            payload={
                "mimeType": "text/plain",
                "body": {"attachmentId": "a1", "size": 101},
            }
        ),
    )
    assert (await EmailTool().execute("read", message_id="a1")).startswith("Error: BODY_TOO_LARGE")
    assert len(requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "data",
    [
        {},
        {"id": "a1", "payload": None},
        message("other-message"),
        message(payload={"headers": None}),
    ],
)
async def test_invalid_or_mismatched_message_is_not_success(mailbox, data):
    state, _ = mailbox
    state["handler"] = lambda request: httpx.Response(200, json=data)
    assert (await EmailTool().execute("read", message_id="a1")).startswith(
        "Error: INVALID_RESPONSE"
    )


@pytest.mark.asyncio
async def test_account_wide_failure_stops_queued_metadata_reads(mailbox, monkeypatch):
    from flowly.integrations import gmail_reader

    monkeypatch.setattr(gmail_reader.asyncio, "sleep", AsyncMock())
    state, requests = mailbox
    state["handler"] = (
        lambda request: httpx.Response(
            200, json={"messages": [{"id": f"a{i}"} for i in range(100)]}
        )
        if request.url.path.endswith("/messages")
        else httpx.Response(429, json={})
    )
    result = json.loads(await EmailTool().execute("inbox", max_results=100))
    assert result["status"] == "partial" and result["listed_count"] == 100
    assert len(result["errors"]) == 100
    assert any(item.get("not_attempted") for item in result["errors"])
    assert len(requests) <= 1 + 5 * 3


@pytest.mark.asyncio
async def test_html_preserves_safe_links_and_table_boundaries(mailbox):
    state, requests = mailbox
    state["handler"] = lambda request: httpx.Response(
        200,
        json=message(
            payload={
                "mimeType": "text/html",
                "body": {
                    "data": encoded(
                        '<table><tr><td>Amount</td><td>100</td></tr></table><a href="https://example.test/invoice">Invoice</a><a href="javascript:alert(1)">Unsafe</a>'
                    )
                },
            }
        ),
    )
    result = json.loads(await EmailTool().execute("read", message_id="a1"))
    assert "Amount\t100" in result["body"]
    assert "Invoice <https://example.test/invoice>" in result["body"]
    assert "javascript:" not in result["body"] and len(requests) == 1
