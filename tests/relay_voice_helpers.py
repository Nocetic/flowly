"""Authenticate a relay sender independently of its account certificate."""
import hashlib
import hmac
import json
import time

from flowly.live_voice.relay_transport import RelayBrowserVerifier

TEST_RELAY_KEY = 'a' * 64


def prepare_relay(channel, socket):
    if channel._ws is socket and channel._relay_authority is not None:
        return
    channel.config.server_id = channel.config.server_id or 'test-server'
    channel._ws = socket
    channel._relay_authority = RelayBrowserVerifier({'version': 1, 'serverId': channel.config.server_id,
                                                    'linkId': 'test-link', 'key': TEST_RELAY_KEY},
                                                   server_id=channel.config.server_id)
    channel._relay_authority_enabled = True
    channel._relay_principals.clear()
    channel._test_relay_sequence = 0


def relay_frame(channel, frame, *, uid, kind='request', expires_at=None):
    channel._test_relay_sequence += 1
    session_id = frame.get('sessionId') or 'test-browser'
    body = json.dumps({'type': 'rpc', **frame, 'sessionId': session_id}, ensure_ascii=False)
    authority = {'version': 1, 'linkId': 'test-link', 'sequence': channel._test_relay_sequence, 'kind': kind,
                 'userId': uid or 'host-browser-account', 'serverId': channel.config.server_id, 'sessionId': session_id,
                 'conversationId': None, 'expiresAt': expires_at if expires_at is not None else int(time.time()) + 300}
    material = ['flowly-relay-browser-v1', authority['linkId'], str(authority['sequence']), str(authority['expiresAt']),
                kind, authority['userId'], authority['serverId'], session_id, '', body]
    mac = hmac.new(bytes.fromhex(TEST_RELAY_KEY), '\0'.join(material).encode('utf-8'), hashlib.sha256).hexdigest()
    return {'type': 'relay.browser', 'authority': authority, 'body': body, 'mac': mac}


async def relay_rpc(channel, socket, frame, *, uid):
    prepare_relay(channel, socket)
    await channel._handle_relay_message(socket, relay_frame(channel, frame, uid=uid))
