"""Voice webhook server for Twilio integration.

Handles:
- HTTP webhooks for call events (incoming, status callbacks)
- WebSocket connections for media streams
"""

import base64
import hashlib
import hmac
import json
from urllib.parse import parse_qsl, urlparse

from loguru import logger
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket

from flowly.config.schema import VoiceWebhookSecurityConfig
from .call_manager import CallManager

MAX_WEBHOOK_BODY_BYTES = 1024 * 1024


def _first_header(headers: dict[str, str], key: str) -> str | None:
    value = headers.get(key)
    if not value:
        return None
    return value.split(",")[0].strip()


def _extract_host(raw_host: str | None) -> str | None:
    if not raw_host:
        return None
    candidate = raw_host.strip()
    if not candidate:
        return None
    if candidate.startswith("["):
        end = candidate.find("]")
        if end == -1:
            return None
        return candidate[1:end].lower()
    if "@" in candidate:
        return None
    return candidate.split(":")[0].lower()


def _normalize_allowed_hosts(cfg: VoiceWebhookSecurityConfig) -> set[str]:
    allowed: set[str] = set()
    for host in cfg.allowed_hosts:
        extracted = _extract_host(host)
        if extracted:
            allowed.add(extracted)
    return allowed


def _is_trusted_proxy(remote_ip: str | None, cfg: VoiceWebhookSecurityConfig) -> bool:
    if not cfg.trusted_proxy_ips:
        return True
    if not remote_ip:
        return False
    return remote_ip in set(cfg.trusted_proxy_ips)


def _resolve_request_origin(
    request: Request,
    webhook_security: VoiceWebhookSecurityConfig,
) -> str | None:
    headers = {k.lower(): v for k, v in request.headers.items()}
    allowed_hosts = _normalize_allowed_hosts(webhook_security)
    has_allowlist = len(allowed_hosts) > 0

    remote_ip = request.client.host if request.client else None
    from_trusted_proxy = _is_trusted_proxy(remote_ip, webhook_security)
    trust_forwarded = (
        (has_allowlist or webhook_security.trust_forwarding_headers) and from_trusted_proxy
    )

    if trust_forwarded:
        proto = _first_header(headers, "x-forwarded-proto") or request.url.scheme or "https"
    else:
        proto = request.url.scheme or "https"

    host_candidates: list[str] = []
    if trust_forwarded:
        for header_key in ("x-forwarded-host", "x-original-host", "ngrok-forwarded-host"):
            candidate = _extract_host(_first_header(headers, header_key))
            if candidate:
                host_candidates.append(candidate)

    host_candidates.append(_extract_host(_first_header(headers, "host")) or "")

    chosen_host: str | None = None
    for candidate in host_candidates:
        if not candidate:
            continue
        if has_allowlist and candidate not in allowed_hosts:
            continue
        chosen_host = candidate
        break

    if not chosen_host:
        return None

    if proto not in {"http", "https"}:
        proto = "https"

    return f"{proto}://{chosen_host}"


def _build_signature_url(
    request: Request,
    webhook_base_url: str,
    webhook_security: VoiceWebhookSecurityConfig,
) -> str | None:
    base = webhook_base_url.strip().rstrip("/")
    if base:
        parsed = urlparse(base)
        if not parsed.scheme or not parsed.netloc:
            return None
        url = f"{base}{request.url.path}"
    else:
        origin = _resolve_request_origin(request, webhook_security)
        if not origin:
            return None
        url = f"{origin}{request.url.path}"

    if request.url.query:
        url = f"{url}?{request.url.query}"
    return url


def _build_stream_url(
    request: Request,
    webhook_base_url: str,
    webhook_security: VoiceWebhookSecurityConfig,
) -> str:
    origin = _resolve_request_origin(request, webhook_security)
    if not origin and webhook_base_url.strip():
        parsed = urlparse(webhook_base_url.strip())
        if parsed.scheme and parsed.netloc:
            origin = f"{parsed.scheme}://{parsed.netloc}"

    if not origin:
        raise ValueError("Unable to resolve public stream origin")

    if origin.startswith("https://"):
        ws_origin = "wss://" + origin[len("https://") :]
    elif origin.startswith("http://"):
        ws_origin = "ws://" + origin[len("http://") :]
    else:
        ws_origin = origin
    return f"{ws_origin}/media-stream"


def _validate_twilio_signature(
    auth_token: str,
    signature: str | None,
    url: str,
    pairs: list[tuple[str, str]],
) -> bool:
    if not signature:
        return False

    data_to_sign = url + "".join(
        f"{key}{value}" for key, value in sorted(pairs, key=lambda item: item[0])
    )
    expected = base64.b64encode(
        hmac.new(auth_token.encode("utf-8"), data_to_sign.encode("utf-8"), hashlib.sha1).digest()
    ).decode("utf-8")
    return hmac.compare_digest(signature, expected)


async def _parse_form_payload(request: Request) -> tuple[str, list[tuple[str, str]], dict[str, str]]:
    body = await request.body()
    if len(body) > MAX_WEBHOOK_BODY_BYTES:
        raise ValueError("PayloadTooLarge")

    raw_body = body.decode("utf-8", errors="ignore")
    pairs = parse_qsl(raw_body, keep_blank_values=True)
    form: dict[str, str] = {}
    for key, value in pairs:
        form[key] = value
    return raw_body, pairs, form


def create_voice_app(
    call_manager: CallManager,
    webhook_base_url: str,
    twilio_auth_token: str,
    webhook_security: VoiceWebhookSecurityConfig | None = None,
    skip_signature_verification: bool = False,
) -> Starlette:
    """Create the voice webhook Starlette application."""

    security = webhook_security or VoiceWebhookSecurityConfig()
    unauthorized_webhook_count = 0

    async def _verify_request(request: Request) -> tuple[dict[str, str] | None, Response | None]:
        nonlocal unauthorized_webhook_count

        if request.method != "POST":
            return None, PlainTextResponse("Method Not Allowed", status_code=405)

        try:
            _, pairs, form = await _parse_form_payload(request)
        except ValueError:
            return None, PlainTextResponse("Payload Too Large", status_code=413)
        except Exception:
            return None, PlainTextResponse("Bad Request", status_code=400)

        if skip_signature_verification:
            return form, None

        verification_url = _build_signature_url(request, webhook_base_url, security)
        signature = request.headers.get("X-Twilio-Signature")
        valid = bool(verification_url) and _validate_twilio_signature(
            auth_token=twilio_auth_token,
            signature=signature,
            url=verification_url or "",
            pairs=pairs,
        )

        if not valid:
            unauthorized_webhook_count += 1
            logger.warning(
                "Unauthorized Twilio webhook rejected: count=%s path=%s client=%s",
                unauthorized_webhook_count,
                request.url.path,
                request.client.host if request.client else "unknown",
            )
            return None, PlainTextResponse("Unauthorized", status_code=401)

        return form, None

    async def handle_incoming_call(request: Request) -> Response:
        form, error = await _verify_request(request)
        if error:
            return error
        assert form is not None

        call_sid = form.get("CallSid", "")
        from_number = form.get("From", "")
        to_number = form.get("To", "")

        logger.info("Incoming call: %s from %s", call_sid, from_number)

        call_manager.create_call(
            call_sid=call_sid,
            from_number=from_number,
            to_number=to_number,
        )

        try:
            stream_url = _build_stream_url(request, webhook_base_url, security)
        except Exception:
            return PlainTextResponse("Webhook origin could not be resolved", status_code=400)

        twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Connect>
        <Stream url="{stream_url}">
            <Parameter name="callSid" value="{call_sid}"/>
        </Stream>
    </Connect>
</Response>"""

        return Response(content=twiml, media_type="application/xml")

    async def handle_outgoing_call(request: Request) -> Response:
        form, error = await _verify_request(request)
        if error:
            return error
        assert form is not None

        call_sid = form.get("CallSid", "")
        call_status = form.get("CallStatus", "")
        from_number = form.get("From", "")
        to_number = form.get("To", "")

        logger.info(
            "Outgoing call webhook: sid=%s status=%s from=%s to=%s",
            call_sid,
            call_status,
            from_number,
            to_number,
        )

        if not call_manager.get_call(call_sid):
            call_manager.create_call(
                call_sid=call_sid,
                from_number=from_number,
                to_number=to_number,
            )

        try:
            stream_url = _build_stream_url(request, webhook_base_url, security)
        except Exception:
            return PlainTextResponse("Webhook origin could not be resolved", status_code=400)

        twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Connect>
        <Stream url="{stream_url}">
            <Parameter name="callSid" value="{call_sid}"/>
        </Stream>
    </Connect>
</Response>"""

        return Response(content=twiml, media_type="application/xml")

    async def handle_call_status(request: Request) -> Response:
        form, error = await _verify_request(request)
        if error:
            return error
        assert form is not None

        call_sid = form.get("CallSid", "")
        call_status = form.get("CallStatus", "")

        logger.info("Call status update: %s -> %s", call_sid, call_status)

        if call_status in ("completed", "failed", "busy", "no-answer", "canceled"):
            await call_manager.handle_call_ended(call_sid)

        return PlainTextResponse("OK")

    async def handle_media_stream(websocket: WebSocket):
        await websocket.accept()

        stream_sid = None
        call_sid = None

        try:
            async for message in websocket.iter_text():
                try:
                    data = json.loads(message)
                except (json.JSONDecodeError, ValueError):
                    logger.warning("Malformed WebSocket message, skipping")
                    continue
                event = data.get("event")

                if event != "media":
                    logger.info("WebSocket event: %s", event)

                if event == "start":
                    stream_sid = data.get("streamSid")
                    start_data = data.get("start", {})
                    call_sid = start_data.get("customParameters", {}).get("callSid")
                    logger.info("Media stream started: %s for call %s", stream_sid, call_sid)

                    if stream_sid:
                        call_manager.register_stream(stream_sid, websocket)

                    if call_sid and stream_sid:
                        await call_manager.handle_call_answered(call_sid, stream_sid)

                elif event == "media":
                    media = data.get("media", {})
                    payload = media.get("payload", "")
                    if call_sid and payload:
                        await call_manager.handle_audio(call_sid, payload)

                elif event == "stop":
                    logger.info("Media stream stopped: %s", stream_sid)
                    if call_sid:
                        await call_manager.handle_call_ended(call_sid)

        except Exception as e:
            logger.error("Media stream error: %s", e)
        finally:
            if stream_sid:
                call_manager.unregister_stream(stream_sid)

    async def health_check(request: Request) -> Response:
        return PlainTextResponse("OK")

    routes = [
        Route("/incoming", handle_incoming_call, methods=["POST"]),
        Route("/outgoing", handle_outgoing_call, methods=["POST"]),
        Route("/status", handle_call_status, methods=["POST"]),
        Route("/health", health_check, methods=["GET"]),
        WebSocketRoute("/media-stream", handle_media_stream),
    ]

    return Starlette(routes=routes)


class TwilioClient:
    """Twilio REST API client for initiating calls."""

    def __init__(
        self,
        account_sid: str,
        auth_token: str,
        phone_number: str,
        webhook_base_url: str,
    ):
        self.account_sid = account_sid
        self.auth_token = auth_token
        self.phone_number = phone_number
        self.webhook_base_url = webhook_base_url.rstrip("/")

    async def make_call(
        self,
        to_number: str,
        call_manager: CallManager,
        telegram_chat_id: str | None = None,
        pending_greeting: str | None = None,
    ) -> str:
        """Initiate an outbound call."""
        import httpx

        if not self.webhook_base_url:
            raise ValueError("integrations.voice.webhook_base_url must be configured")

        url = f"https://api.twilio.com/2010-04-01/Accounts/{self.account_sid}/Calls.json"

        async with httpx.AsyncClient() as client:
            response = await client.post(
                url,
                auth=(self.account_sid, self.auth_token),
                data={
                    "To": to_number,
                    "From": self.phone_number,
                    "Url": f"{self.webhook_base_url}/outgoing",
                    "StatusCallback": f"{self.webhook_base_url}/status",
                    "StatusCallbackEvent": ["initiated", "ringing", "answered", "completed"],
                },
            )

            if response.status_code not in (200, 201):
                raise Exception(f"Twilio API error: HTTP {response.status_code}")

            result = response.json()
            call_sid = result["sid"]

            call_manager.create_call(
                call_sid=call_sid,
                from_number=self.phone_number,
                to_number=to_number,
                telegram_chat_id=telegram_chat_id,
                pending_greeting=pending_greeting,
            )

            logger.info("Outbound call initiated: %s to %s", call_sid, to_number)
            return call_sid

    async def end_call(self, call_sid: str) -> bool:
        """End an active call."""
        import httpx

        url = f"https://api.twilio.com/2010-04-01/Accounts/{self.account_sid}/Calls/{call_sid}.json"

        async with httpx.AsyncClient() as client:
            response = await client.post(
                url,
                auth=(self.account_sid, self.auth_token),
                data={"Status": "completed"},
            )

            if response.status_code != 200:
                logger.error("Failed to end call: HTTP %s", response.status_code)
                return False

            logger.info("Call ended via API: %s", call_sid)
            return True
