"""Read-only work outputs on the pinned worker's own filesystem and store."""
from __future__ import annotations

import base64
import hashlib
import mimetypes
import os
import re
import stat
from pathlib import Path
from typing import Callable

from flowly.artifacts.context import is_internal_context_artifact
from flowly.live_voice.sessions import VoiceError, bounded_text, integer
from flowly.session.commands import validate_chat_target

MAX_BYTES = 32 * 1024 * 1024
WINDOW_BYTES = 256 * 1024
_TYPES = {'html': ('text/html', '.html'), 'svg': ('image/svg+xml', '.svg'),
          'markdown': ('text/markdown', '.md'), 'csv': ('text/csv', '.csv'),
          'json': ('application/json', '.json'), 'code': ('text/plain', '.txt'),
          'mermaid': ('text/plain', '.mmd'), 'latex': ('text/plain', '.tex'),
          'form': ('text/html', '.html'), 'chart': ('text/html', '.html')}
_SENSITIVE_NAMES = {'.netrc', '_netrc', '.pgpass', '.git-credentials', '.htpasswd',
                    'credentials', 'id_rsa', 'id_dsa', 'id_ecdsa', 'id_ed25519'}
_SENSITIVE_DIRS = {'.ssh', '.aws', '.gnupg', '.kube', '.docker', '.password-store', 'keychains'}
_SENSITIVE_EXT = {'.pem', '.p12', '.pfx', '.keystore', '.jks', '.kdbx', '.keychain', '.keychain-db'}


def validate_output_request(method: str, params: dict) -> dict:
    permitted = {'sessionKey', 'expectedBotId'} | (
        {'offset', 'limit'} if method == 'chat.outputs.list' else
        {'source', 'path', 'id', 'offset', 'length', 'expectedRevision'})
    if set(params) - permitted:
        raise VoiceError('INVALID_PARAMS', 'Unknown work output parameters.')
    session_key = bounded_text(params.get('sessionKey'), 'sessionKey', maximum=200)
    if not re.fullmatch(r'desktop:voice-work:[A-Za-z0-9_.:-]{1,128}', session_key):
        raise VoiceError('INVALID_PARAMS', 'A work conversation is required.')
    safe = {**params, 'sessionKey': session_key}
    safe['offset'] = integer(params.get('offset', 0), 'offset', maximum=MAX_BYTES)
    if method == 'chat.outputs.list':
        safe['limit'] = integer(params.get('limit', 50), 'limit', minimum=1, maximum=100)
        return safe
    source = params.get('source')
    if source not in {'file', 'artifact'}:
        raise VoiceError('INVALID_PARAMS', 'Choose a file or artifact.')
    key = 'path' if source == 'file' else 'id'
    if ('id' if key == 'path' else 'path') in params:
        raise VoiceError('INVALID_PARAMS', 'Choose one output source.')
    safe[key] = bounded_text(params.get(key), key, maximum=4096 if key == 'path' else 128)
    if any(ord(c) < 32 or ord(c) == 127 for c in safe[key]):
        raise VoiceError('INVALID_PARAMS', 'Invalid output reference.')
    safe['length'] = integer(params.get('length', WINDOW_BYTES), 'length', minimum=1, maximum=WINDOW_BYTES)
    if safe['offset'] or params.get('expectedRevision') is not None:
        revision = bounded_text(params.get('expectedRevision'), 'expectedRevision', maximum=64)
        if not re.fullmatch('[a-f0-9]{64}', revision):
            raise VoiceError('INVALID_PARAMS', 'A valid output revision is required.')
    return safe


def _signature(info: os.stat_result) -> tuple:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _open_regular(path: Path) -> int:
    """Walk canonical POSIX components without following replacement links."""
    if os.name == 'posix':
        directory = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
        try:
            for part in path.parts[1:-1]:
                following = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
                os.close(directory)
                directory = following
            return os.open(path.name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=directory)
        finally:
            os.close(directory)
    descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_BINARY', 0))
    try:
        # Python's dir_fd operations are unavailable on Windows. Check the
        # actual opened handle, rather than trusting a junction before open.
        import ctypes
        import msvcrt
        from ctypes import wintypes

        get_path = ctypes.WinDLL('kernel32', use_last_error=True).GetFinalPathNameByHandleW
        get_path.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
        get_path.restype = wintypes.DWORD
        buffer = ctypes.create_unicode_buffer(32768)
        length = get_path(msvcrt.get_osfhandle(descriptor), buffer, len(buffer), 0)
        if not 0 < length < len(buffer):
            raise OSError('Cannot verify the opened file.')
        actual = buffer.value
        if actual.startswith('\\\\?\\UNC\\'):
            actual = '\\\\' + actual[8:]
        elif actual.startswith('\\\\?\\'):
            actual = actual[4:]
        if Path(actual) != path:
            raise OSError('The opened path changed.')
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


class WorkOutputs:
    def __init__(self, workspace: Path, sessions, artifacts: Callable):
        self.workspace = workspace.resolve()
        self.sessions = sessions
        self.artifacts = artifacts

    def _scope(self, method: str, params: dict) -> dict:
        safe = validate_output_request(method, params)
        if not safe.get('expectedBotId'):
            raise VoiceError('INVALID_PARAMS', 'The selected agent identity is required.')
        validate_chat_target(safe)
        key = safe['sessionKey']
        from flowly.session.ownership import require_session_access, require_session_file

        require_session_file(self.sessions._get_session_path(key), key)
        cached = self.sessions._cache.get(key)
        if cached is not None:
            require_session_access(key, cached.metadata)
        if self.sessions._cache.get(key) is None and self.sessions._load(key) is None:
            raise VoiceError('NOT_FOUND', 'Work conversation not found.')
        return safe

    def list(self, params: dict) -> dict:
        safe = validate_output_request('chat.outputs.list', params)
        with self.sessions._session_write_lock(safe['sessionKey']):
            safe = self._scope('chat.outputs.list', params)
        # The store locks origin and output sessions in a stable order. Do not
        # hold only the output lock while it acquires a different origin lock.
        rows = self.artifacts().session_summaries(safe['sessionKey'], safe['offset'], safe['limit'], include_internal=False)
        artifacts = [self._summary(row) for row in rows]
        return {'sessionKey': safe['sessionKey'], 'artifacts': artifacts,
                'nextOffset': safe['offset'] + safe['limit'] if len(rows) == safe['limit'] else None}

    @staticmethod
    def _summary(artifact: dict) -> dict:
        return {'id': artifact['id'], 'title': str(artifact.get('title') or '')[:200],
                'type': artifact['type'], 'version': artifact['version'], 'updatedAt': artifact['updated_at']}

    def _file_path(self, raw: str) -> Path:
        from flowly.agent.tools.filesystem import _is_read_allowed
        from flowly.profile import current_profile_name

        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            candidate = self.workspace / candidate
        resolved = candidate.resolve(strict=True)
        roots = [self.workspace]
        if current_profile_name() == 'default':
            roots.extend(Path.home() / name for name in ('Downloads', 'Desktop', 'Documents'))
        if not any(resolved.is_relative_to(root.resolve()) for root in roots) or not _is_read_allowed(resolved, self.workspace):
            raise VoiceError('OUTPUT_UNAVAILABLE', 'This file is outside this agent’s output folders.')
        name = resolved.name.lower()
        if (name in _SENSITIVE_NAMES or name == '.env' or name.startswith('.env.')
                or resolved.suffix.lower() in _SENSITIVE_EXT
                or any(part.lower() in _SENSITIVE_DIRS for part in resolved.parts[:-1])):
            raise VoiceError('OUTPUT_UNAVAILABLE', 'Credential files are not work output previews.')
        return resolved

    @staticmethod
    def _window(safe: dict, data: bytes, size: int, revision: str, **identity) -> dict:
        return {'sessionKey': safe['sessionKey'], 'source': safe['source'], **identity,
                'size': size, 'revision': revision, 'offset': safe['offset'],
                'eof': safe['offset'] + len(data) == size, 'data': base64.b64encode(data).decode('ascii')}

    @staticmethod
    def _validate_window(safe: dict, size: int, revision: str) -> None:
        if size > MAX_BYTES:
            raise VoiceError('OUTPUT_TOO_LARGE', 'File previews support outputs up to 32 MB.')
        if safe['offset'] > size:
            raise VoiceError('INVALID_PARAMS', 'Output offset exceeds its size.')
        if safe.get('expectedRevision') is not None and safe['expectedRevision'] != revision:
            raise VoiceError('OUTPUT_CHANGED', 'This output changed. Open its latest version.')

    def read(self, params: dict) -> dict:
        safe = validate_output_request('chat.outputs.read', params)
        with self.sessions._session_write_lock(safe['sessionKey']):
            if safe['source'] != 'artifact':
                return self._read_unlocked(params)
            safe = self._scope('chat.outputs.read', params)
        return self._read_artifact(safe)

    def _read_artifact(self, safe: dict) -> dict:
        artifact = self.artifacts().get_session_output(safe['id'], safe['sessionKey'])
        if not artifact or is_internal_context_artifact(artifact):
            raise VoiceError('NOT_FOUND', 'Output not found in this conversation.')
        content = str(artifact.get('content') or '')
        if len(content) > MAX_BYTES:
            raise VoiceError('OUTPUT_TOO_LARGE', 'Output previews support up to 32 MB.')
        data = content.encode('utf-8')
        revision = hashlib.sha256(str(artifact['version']).encode() + b'\0' + data).hexdigest()
        self._validate_window(safe, len(data), revision)
        mime, extension = _TYPES.get(artifact['type'], ('text/plain', '.txt'))
        title = re.sub(r'[/\\\x00-\x1f]', '-', artifact.get('title') or artifact['id'])[:180]
        return self._window(safe, data[safe['offset']:safe['offset'] + safe['length']], len(data), revision,
                            id=artifact['id'], fileName=title + extension, mimeType=mime,
                            artifactType=artifact['type'], version=artifact['version'])

    def _read_unlocked(self, params: dict) -> dict:
        safe = self._scope('chat.outputs.read', params)
        try:
            path = self._file_path(safe['path'])
            descriptor = _open_regular(path)
            with os.fdopen(descriptor, 'rb') as stream:
                before = os.fstat(stream.fileno())
                if not stat.S_ISREG(before.st_mode):
                    raise VoiceError('OUTPUT_UNAVAILABLE', 'Only regular files can be previewed.')
                revision = hashlib.sha256(repr(_signature(before)).encode()).hexdigest()
                self._validate_window(safe, before.st_size, revision)
                stream.seek(safe['offset'])
                data = stream.read(safe['length'])
                if (_signature(before) != _signature(os.fstat(stream.fileno()))
                        or len(data) != min(safe['length'], before.st_size - safe['offset'])):
                    raise VoiceError('OUTPUT_CHANGED', 'This output changed. Open its latest version.')
                mime = mimetypes.guess_type(path.name)[0] or 'application/octet-stream'
                return self._window(safe, data, before.st_size, revision, path=str(path), fileName=path.name, mimeType=mime)
        except (OSError, RuntimeError, ValueError) as exc:
            if isinstance(exc, VoiceError):
                raise
            raise VoiceError('OUTPUT_UNAVAILABLE', 'This file is unavailable on its agent.') from exc
