"""Private media must survive cache loss without becoming public files."""
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from flowly.live_voice.authority import HOST_OWNER, RequestOwner, request_owner_scope
from flowly.live_voice.events import EventAccess, event_access_scope
from flowly.media.authority import capture_media_access, media_visible, publish_media_file
from flowly.media.library import MediaLibrary
from flowly.media.serving import read_media_window, resolve_media_id
from flowly.session.manager import SessionManager
from flowly.session.ownership import SessionAccessError

A, B = RequestOwner('media-account-a'), RequestOwner('media-account-b')
KEY = 'web:private-media-session'


@pytest.fixture
def source(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    monkeypatch.setenv('FLOWLY_HOME', str(home))
    sessions = SessionManager(tmp_path / 'workspace')
    with request_owner_scope(A):
        session = sessions.get_or_create(KEY)
        session.metadata['voiceOwner'] = {'kind': 'account', 'uid': A.uid}
        sessions.save(session)
        access = EventAccess.capture(KEY)
    media = home / 'media'
    media.mkdir()
    return media, access


def publish(source, name='sample.png'):
    media, access = source
    temporary = media / '.download.part'
    temporary.write_bytes(b'private media contents')
    with request_owner_scope(A):
        return publish_media_file(temporary, media / name, access)


def test_publication_precedes_any_visible_bytes_and_denies_other_accounts(source, monkeypatch):
    media, _ = source
    import os

    link = os.link
    observed = []

    def inspect(temporary, destination):
        assert not destination.exists()
        record = media / '.authority' / (destination.name + '.json')
        assert json.loads(record.read_text())['scopes'][0]['owner']['uid'] == A.uid
        assert record.stat().st_mode & 0o777 == 0o600
        observed.append(True)
        link(temporary, destination)

    monkeypatch.setattr(os, 'link', inspect)
    path = publish(source)
    assert observed == [True]
    assert not (media / '.download.part').exists()
    with request_owner_scope(A):
        assert read_media_window(path.name, length=100, media_dir=media).data == b'private media contents'
    for owner in [B, HOST_OWNER]:
        with request_owner_scope(owner):
            assert resolve_media_id(path.name, media) == (None, 'not found', 404)
            assert read_media_window(path.name, length=0, media_dir=media).error == 'not_found'


@pytest.mark.parametrize('damage', ['missing', 'malformed', 'too_large', 'public_owner', 'replaced_file'])
def test_missing_or_invalid_authority_and_replacement_never_downgrade(source, damage):
    media, _ = source
    path = publish(source)
    record = media / '.authority' / (path.name + '.json')
    if damage == 'missing':
        record.unlink()
    elif damage == 'malformed':
        record.write_text('{')
    elif damage == 'too_large':
        record.write_text(' ' * 65537)
    elif damage == 'public_owner':
        payload = json.loads(record.read_text())
        payload['scopes'] = []
        payload['owner'] = None
        record.write_text(json.dumps(payload))
    else:
        replacement = media / '.replacement'
        replacement.write_bytes(path.read_bytes())
        replacement.replace(path)
    for owner in [None, A, B, HOST_OWNER]:
        with request_owner_scope(owner):
            assert not media_visible(path)


def test_cache_loss_rebuild_filters_before_search_pagination_stats_and_mutations(source):
    media, _ = source
    path = publish(source)
    (media / 'shared.png').write_bytes(b'shared')
    database = media.parent / 'media.sqlite'
    for rebuild in range(2):
        if rebuild:
            database.unlink()
        library = MediaLibrary(database, media)
        try:
            library.reconcile(probe_new=False)
            thumbs = media / 'thumbs'
            thumbs.mkdir(exist_ok=True)
            (thumbs / (path.name + '.jpg')).write_bytes(b'private thumbnail')
            (thumbs / 'shared.png.jpg').write_bytes(b'shared thumbnail')
            with request_owner_scope(B):
                items, total = library.list(limit=1, with_thumbs=True)
                assert total == 1 and items[0]['mediaId'] == 'shared.png'
                assert library.list(search='sample') == ([], 0)
                assert library.list(offset=1) == ([], 1)
                assert library.get(path.name) is None
                assert library.star(path.name) is None
                assert not library.delete(path.name)
                assert path.exists()
                stats = library.stats()
                assert stats['totalItems'] == 1 and stats['totalBytes'] == 6
                assert stats['thumbnailBytes'] == len(b'shared thumbnail')
            with request_owner_scope(A):
                assert library.list()[1] == 2
                assert library.get(path.name)['starred'] is False
        finally:
            library.close()


def test_original_scope_survives_restart_and_canonical_reassignment(source):
    media, access = source
    path = publish(source)
    canonical = access.scopes[0].path
    canonical.write_text(canonical.read_text().replace(A.uid, B.uid))
    for owner in [None, A, B, HOST_OWNER]:
        with request_owner_scope(owner):
            assert not media_visible(path)
    with request_owner_scope(B):
        library = MediaLibrary(media.parent / 'fresh.sqlite', media)
        try:
            library.reconcile(probe_new=False)
            assert library.list() == ([], 0)
        finally:
            library.close()


def test_captured_producer_cannot_publish_after_reassignment_or_as_another_account(source):
    media, access = source
    temporary = media / '.download'
    temporary.write_bytes(b'private')
    with request_owner_scope(B), pytest.raises(SessionAccessError):
        publish_media_file(temporary, media / 'one.png', access)
    canonical = access.scopes[0].path
    canonical.write_text(canonical.read_text().replace(A.uid, B.uid))
    with request_owner_scope(A), pytest.raises(SessionAccessError):
        publish_media_file(temporary, media / 'one.png', access)
    assert list(media.iterdir()) == [temporary]


def test_symlink_alias_does_not_bypass_private_authority(source):
    media, _ = source
    path = publish(source)
    alias = media / 'innocent.png'
    alias.symlink_to(path)
    with request_owner_scope(B):
        assert resolve_media_id(alias.name, media) == (None, 'not found', 404)
    with request_owner_scope(A):
        assert resolve_media_id(alias.name, media)[0] == path


def test_failed_publication_cleans_only_its_own_sidecar_and_never_overwrites(source, monkeypatch):
    import os

    media, _ = source
    path = publish(source)
    record = media / '.authority' / (path.name + '.json')
    original = record.read_bytes()
    with pytest.raises(FileExistsError):
        publish(source)
    assert record.read_bytes() == original
    assert path.read_bytes() == b'private media contents'

    def fail(*args):
        raise OSError('injected publication failure')

    monkeypatch.setattr(os, 'link', fail)
    with pytest.raises(OSError, match='injected'):
        publish(source, 'failed.png')
    assert sorted(p.name for p in (media / '.authority').iterdir()) == [record.name]


def test_capture_clones_original_scope_and_rejects_profile_projection(source):
    media, access = source
    with request_owner_scope(A), event_access_scope(access):
        frozen = capture_media_access()
    assert frozen.scopes[0].owner is not access.scopes[0].owner
    other = media.parent / 'other' / 'media'
    other.mkdir(parents=True)
    temp = other / '.download'
    temp.write_bytes(b'private')
    with request_owner_scope(A), pytest.raises(SessionAccessError):
        publish_media_file(temp, other / 'image.png', frozen)


def test_sessionless_account_publication_remains_private(source):
    media, _ = source
    with request_owner_scope(A):
        access = capture_media_access()
        temporary = media / '.audio'
        temporary.write_bytes(b'audio')
        path = publish_media_file(temporary, media / 'audio.wav', access)
        assert media_visible(path)
    with request_owner_scope(B):
        assert not media_visible(path)


@pytest.mark.asyncio
async def test_legacy_relay_media_fetch_cannot_inherit_account_authority(source):
    from flowly.channels.web import WebChannel

    path = publish(source)
    channel = object.__new__(WebChannel)
    socket = type('Socket', (), {'send': AsyncMock()})()
    with request_owner_scope(A):
        await channel._serve_media_fetch(socket, {
            'requestId': 'fetch-1', 'mediaId': path.name, 'length': 100,
            'voiceAccess': 'unverified-client-input', 'uid': A.uid,
        })
    reply = json.loads(socket.send.call_args.args[0])
    assert reply == {'type': 'media.result', 'requestId': 'fetch-1', 'ok': False, 'error': 'not_found'}


@pytest.mark.asyncio
@pytest.mark.parametrize('reassign_during_download', [False, True])
async def test_actual_downloader_publishes_original_authority_or_rejects_reassignment(
    source, monkeypatch, reassign_during_download,
):
    from flowly.media import generate

    media, access = source

    class Response:
        status_code = 200
        headers = {'content-type': 'video/mp4'}

        async def aiter_bytes(self):
            yield b'video output'
            if reassign_during_download:
                canonical = access.scopes[0].path
                canonical.write_text(canonical.read_text().replace(A.uid, B.uid))

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    class Client(Response):
        def stream(self, *args, **kwargs):
            return Response()

    monkeypatch.setattr(generate.httpx, 'AsyncClient', lambda **kwargs: Client())
    with request_owner_scope(A), event_access_scope(access):
        if reassign_during_download:
            with pytest.raises(SessionAccessError):
                await generate.download_output('https://provider.test/video.mp4', kind='video')
            assert list(media.iterdir()) == []
            return
        path = await generate.download_output('https://provider.test/video.mp4', kind='video')
        assert path.read_bytes() == b'video output'
        assert media_visible(path)
    with request_owner_scope(B):
        assert not read_media_window(path.name, length=100).ok


@pytest.mark.asyncio
async def test_actual_http_routes_do_not_treat_a_playback_ticket_as_account_authority(source):
    from aiohttp.test_utils import TestClient, TestServer

    from flowly.gateway.auth import TOKEN_HEADER
    from flowly.gateway.server import GatewayServer

    path = publish(source, 'clip.mp4')
    token = 'test-static-gateway-token'
    gateway = GatewayServer(host='127.0.0.1', port=0, auth_token=token)
    async with TestClient(TestServer(gateway._create_app())) as client:
        response = await client.get('/api/media', params={'id': path.name}, headers={TOKEN_HEADER: token})
        assert response.status == 404
        response = await client.post('/api/media/tickets', json={'id': path.name}, headers={TOKEN_HEADER: token})
        assert response.status == 404
        # Even a valid legacy ticket has no private account identity.
        ticket = gateway._media_ticket_store.mint(path.name)
        response = await client.get('/api/media/stream', params={'id': path.name, 'ticket': ticket})
        assert response.status == 404
        assert b'private media contents' not in await response.read()


@pytest.mark.parametrize('transport', ['gateway', 'relay'])
def test_attachment_writers_publish_private_bytes_and_reject_other_account_paths(source, transport):
    from flowly.channels.web import _save_attachments as relay_save
    from flowly.gateway.server import _save_attachments as gateway_save

    save = gateway_save if transport == 'gateway' else relay_save
    media, access = source
    with request_owner_scope(A), event_access_scope(access):
        paths = save([{'content': 'cHJpdmF0ZQ==', 'fileName': 'photo.png'}], media)
        path = Path(paths[0])
        assert path.read_bytes() == b'private'
        assert media_visible(path)
        assert save([{'filePath': str(path)}], media) == paths
    alias = media / 'alias.png'
    alias.symlink_to(path)
    with request_owner_scope(B):
        for input_path in (path, alias):
            with pytest.raises(SessionAccessError):
                save([{'filePath': str(input_path)}], media)
    assert not list(media.glob('*.part'))


@pytest.mark.asyncio
@pytest.mark.parametrize('transport', ['gateway', 'relay'])
async def test_chat_ingress_pins_attachment_to_the_accepted_command_scope(source, transport):
    import asyncio

    from flowly.bus.queue import MessageBus
    from flowly.channels.web import WebChannel
    from flowly.config.schema import WebChannelConfig
    from flowly.gateway.server import GatewayServer

    media, _ = source
    socket = type('Socket', (), {'send': AsyncMock(), 'send_json': AsyncMock(), 'closed': False})()
    params = {'sessionKey': KEY, 'idempotencyKey': 'media-command', 'message': 'Inspect this',
              'attachments': [{'content': 'cHJpdmF0ZQ==', 'fileName': 'photo.png'}]}
    if transport == 'gateway':
        surface = GatewayServer(on_chat_message=AsyncMock(return_value=('done', {})))

        async def send():
            await surface._ws_rpc_chat_send(socket, 'client', 'request', params)
    else:
        surface = WebChannel(WebChannelConfig(enabled=True), MessageBus())

        async def send():
            # Authentication is covered separately. Enter the actual dispatcher
            # with its verified account, then inspect the persisted output.
            await surface._dispatch_rpc(socket, {'id': 'request', 'method': 'chat.send',
                                                'sessionId': 'browser', 'params': params})

    try:
        with request_owner_scope(A):
            await send()
            await asyncio.gather(*list(surface._active_tasks.values()))
            records = list((media / '.authority').glob('*.json'))
            assert len(records) == 1
            saved = json.loads(records[0].read_text())
            assert saved['scopes'] == [{'key': KEY, 'owner': {'kind': 'account', 'uid': A.uid}}]
            await send()
            assert list((media / '.authority').glob('*.json')) == records
    finally:
        await surface.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize('provider', ['fal', 'elevenlabs'])
@pytest.mark.parametrize('reassign', [False, True])
async def test_byte_providers_preserve_original_scope_through_provider_io(source, monkeypatch, provider, reassign):
    from types import SimpleNamespace

    import httpx

    from flowly.media import fal
    from flowly.voice import generate
    from flowly.voice.providers import elevenlabs

    media, access = source

    def finish_bytes():
        if reassign:
            canonical = access.scopes[0].path
            canonical.write_text(canonical.read_text().replace(A.uid, B.uid))
        return b'generated bytes'

    if provider == 'fal':
        def handler(request):
            if request.method == 'POST':
                return httpx.Response(200, json={'images': [{'url': 'https://provider.test/image.png'}]})
            return httpx.Response(200, content=finish_bytes())

        client = httpx.AsyncClient
        monkeypatch.setattr(fal.httpx, 'AsyncClient', lambda **kwargs: client(
            **kwargs, transport=httpx.MockTransport(handler)))

        async def run():
            result = await fal.generate_image(api_key='test', model='test', prompt='private prompt')
            return Path(result['paths'][0])
    else:
        async def synthesize(*args, **kwargs):
            return finish_bytes()

        monkeypatch.setattr(elevenlabs, 'synthesize_speech', synthesize)
        monkeypatch.setattr(generate, '_finish', AsyncMock(side_effect=lambda path, **kwargs: path))
        settings = SimpleNamespace(speech_ready=True, model_id='test', api_key='test', voice_id='test')

        async def run():
            return await generate.generate_elevenlabs(settings, mode='speech', prompt='private words')

    with request_owner_scope(A), event_access_scope(access):
        if reassign:
            with pytest.raises(SessionAccessError):
                await run()
            assert not list(media.iterdir())
            return
        path = await run()
        assert path.read_bytes() == b'generated bytes'
        assert media_visible(path)
    with request_owner_scope(B):
        assert not read_media_window(path.name, length=100, media_dir=media).ok
