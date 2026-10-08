import socket
from unittest.mock import Mock

import pytest

from flowly.desktop_runtime.authority import ScreenError
from flowly.desktop_runtime.capture_service import CaptureService
from flowly.desktop_runtime.input_service import _receive, _send

pytestmark = pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="private Unix transport")


@pytest.fixture
def capture_service(tmp_path):
    observers, viewers = Mock(), Mock()
    observers.is_current.side_effect = lambda viewer, stream: (viewer, stream) == (
        "owner",
        "current",
    )
    viewers.available.side_effect = (
        lambda viewer, generation: viewer == "owner" and generation == "generation"
    )
    listener = socket.socket(socket.AF_UNIX)
    listener.bind(str(tmp_path / "rfb.sock"))
    listener.listen(4)
    listener.settimeout(0.1)
    service = CaptureService(
        "generation", tmp_path, tmp_path / "rfb.sock", observers=observers, viewers=viewers
    )
    try:
        yield service, listener
    finally:
        service.close()
        listener.close()
        assert not service.path.exists()
        assert not service.is_alive


@pytest.mark.parametrize(
    "override",
    [
        {"viewer": "foreign"},
        {"stream": "stale"},
        {"generation": "previous"},
        {"stream": None},
        {"extra": True},
    ],
)
def test_invalid_capture_never_connects_to_xvnc(capture_service, override):
    service, listener = capture_service
    with socket.socket(socket.AF_UNIX) as connection:
        connection.connect(str(service.path))
        _send(
            connection,
            {"viewer": "owner", "stream": "current", "generation": "generation", **override},
        )
        assert _receive(connection) == {"ok": False}
    with pytest.raises(socket.timeout):
        listener.accept()


def test_capture_claim_requires_live_matching_observer_and_viewer(capture_service):
    service, listener = capture_service
    with socket.socket(socket.AF_UNIX) as connection:
        connection.connect(str(service.path))
        _send(connection, {"viewer": "owner", "stream": "current", "generation": "generation"})
        assert _receive(connection) == {"ok": True}
        server, _ = listener.accept()
        with server:
            for viewer, stream in (("foreign", "current"), ("owner", "stale")):
                with pytest.raises(ScreenError, match="VIEWER_REVOKED"):
                    service.claim(viewer, stream)
            service.viewers.available.side_effect = None
            service.viewers.available.return_value = False
            with pytest.raises(ScreenError, match="VIEWER_REVOKED"):
                service.claim("owner", "current")


def test_duplicate_capture_is_rejected_and_original_socket_survives(capture_service):
    service, listener = capture_service
    registration = {"viewer": "owner", "stream": "current", "generation": "generation"}
    with socket.socket(socket.AF_UNIX) as first, socket.socket(socket.AF_UNIX) as second:
        first.connect(str(service.path))
        _send(first, registration)
        assert _receive(first) == {"ok": True}
        backend, _ = listener.accept()
        with backend:
            second.connect(str(service.path))
            _send(second, registration)
            assert _receive(second) == {"ok": False}
            rejected, _ = listener.accept()
            with rejected:
                assert rejected.recv(1) == b""
            backend.sendall(b"RFB 003.008\n")
            first.settimeout(2)
            assert first.recv(12) == b"RFB 003.008\n"
