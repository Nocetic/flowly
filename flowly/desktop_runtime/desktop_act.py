"""One desktop action on a profile screen, done inside its session (D10).

The agent's ``computer`` tool drives the whole desktop the person sees, not
only the browser tab. Like the desktop still (``desktop_still``), the screen's
supervisor serves it on a private socket and runs it inside the X session as
the screen's application account, so the agent needs no display key. The
gateway asks only as one of a run's screen actions, and the supervisor asks
the authority again for every action: nothing is done while the person has
control, whoever reaches the socket.

Requests are validated into fixed argument lists here; nothing a model sends
reaches a shell.
"""

from __future__ import annotations

import json
import re
import socket
import struct
import threading
from pathlib import Path
from typing import Callable

from flowly.desktop_runtime.authority import ScreenError

MAX_REQUEST_BYTES = 16 * 1024
MAX_TEXT = 4000
MAX_COORDINATE = 32767
_KEY = re.compile(r"^[A-Za-z0-9_+]{1,100}$")
_BUTTONS = {"left": "1", "middle": "2", "right": "3"}
_SCROLL = {"up": "4", "down": "5", "left": "6", "right": "7"}
#: The dock's own applications (desktop_defaults); no free-form command.
LAUNCHERS = {
    "terminal": ["/usr/bin/xfce4-terminal", "--disable-server", "--command=/bin/bash"],
    "settings": ["/usr/bin/xfce4-settings-manager"],
}
ACTIONS = frozenset({"click", "move", "drag", "scroll", "type", "key", "open_app"})
#: Actions that aim at a point: planned on a picture, so one must have been taken.
POINTED = frozenset({"click", "move", "drag", "scroll"})

# The epoch of each run's latest whole-desktop picture (screenshot or computer
# tool), so a pointed action is never planned on a picture from before a
# takeover. Bounded; a forgotten run just has to look again.
_OBSERVED: dict[str, int] = {}


def note_observation(run_id: str, epoch: int) -> None:
    _OBSERVED.pop(run_id, None)
    _OBSERVED[run_id] = epoch
    while len(_OBSERVED) > 256:
        _OBSERVED.pop(next(iter(_OBSERVED)))


def observed_epoch(run_id: str) -> int | None:
    return _OBSERVED.get(run_id)

# Run as the application account with the session's DISPLAY/XAUTHORITY:
# python3 -I -c ACT_SOURCE <request>. Prints "ok" when the action ran.
ACT_SOURCE = r'''
import json
import subprocess
import sys

request = json.loads(sys.argv[1])
quiet = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
if request["kind"] == "launch":
    subprocess.Popen(request["argv"], start_new_session=True, **quiet)
    print("ok")
else:
    done = subprocess.run(["xdotool", *request["args"]], timeout=8, **quiet)
    print("ok" if done.returncode == 0 else "failed")
'''


def _coordinate(params: dict, name: str) -> str:
    value = params.get(name)
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_COORDINATE:
        raise ScreenError("INVALID_PARAMS")
    return str(value)


def _count(params: dict, name: str, default: int, maximum: int) -> str:
    value = params.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise ScreenError("INVALID_PARAMS")
    return str(value)


def act_request(action: object, params: object) -> dict:
    """A validated helper request for ``action``, or ScreenError("INVALID_PARAMS")."""
    if action not in ACTIONS or not isinstance(params, dict):
        raise ScreenError("INVALID_PARAMS")
    if action == "open_app":
        argv = LAUNCHERS.get(params.get("name"))  # type: ignore[arg-type]
        if argv is None:
            raise ScreenError("INVALID_PARAMS")
        return {"kind": "launch", "argv": argv}
    if action == "type":
        text = params.get("text")
        if not isinstance(text, str) or not text or len(text) > MAX_TEXT or "\x00" in text:
            raise ScreenError("INVALID_PARAMS")
        return {"kind": "xdotool", "args": ["type", "--clearmodifiers", "--delay", "12", "--", text]}
    if action == "key":
        key = params.get("key")
        if not isinstance(key, str) or not _KEY.fullmatch(key):
            raise ScreenError("INVALID_PARAMS")
        return {"kind": "xdotool", "args": ["key", "--clearmodifiers", key]}
    x, y = _coordinate(params, "x"), _coordinate(params, "y")
    move = ["mousemove", "--sync", x, y]
    if action == "move":
        return {"kind": "xdotool", "args": move}
    if action == "click":
        button = _BUTTONS.get(params.get("button", "left"))  # type: ignore[arg-type]
        if button is None:
            raise ScreenError("INVALID_PARAMS")
        return {"kind": "xdotool", "args": [*move, "click", "--repeat", _count(params, "count", 1, 3), button]}
    if action == "drag":
        to = ["mousemove", "--sync", _coordinate(params, "to_x"), _coordinate(params, "to_y")]
        return {"kind": "xdotool", "args": [*move, "mousedown", "1", *to, "mouseup", "1"]}
    direction = _SCROLL.get(params.get("direction"))  # type: ignore[arg-type]
    if direction is None:
        raise ScreenError("INVALID_PARAMS")
    return {"kind": "xdotool", "args": [*move, "click", "--repeat", _count(params, "amount", 3, 30), direction]}


def _receive(connection: socket.socket, size: int) -> bytes:
    data = bytearray()
    while len(data) < size:
        part = connection.recv(min(size - len(data), 1 << 16))
        if not part:
            raise ScreenError("SCREEN_ACT_FAILED")
        data += part
    return bytes(data)


def _send(connection: socket.socket, value: dict) -> None:
    body = json.dumps(value).encode()
    connection.sendall(struct.pack("!I", len(body)) + body)


def _read(connection: socket.socket) -> dict:
    (size,) = struct.unpack("!I", _receive(connection, 4))
    if not 0 < size <= MAX_REQUEST_BYTES:
        raise ScreenError("SCREEN_ACT_FAILED")
    value = json.loads(_receive(connection, size))
    if not isinstance(value, dict):
        raise ScreenError("SCREEN_ACT_FAILED")
    return value


class ActService:
    """Supervisor side: one action per connection, one at a time.

    ``allowed`` is asked again here for every action, whoever connected (a
    root shell can reach the socket): nothing is done while the person has
    control. ``run`` executes ACT_SOURCE in the session as the application
    account and returns its stdout.
    """

    def __init__(self, directory: Path, run: Callable[[str], bytes], allowed: Callable[[], bool]):
        self.path = directory / "act.sock"
        self._run = run
        self._allowed = allowed
        self._lock = threading.Lock()
        self._stopping = threading.Event()
        self._listener = socket.socket(socket.AF_UNIX)
        try:
            self._listener.bind(str(self.path))
            self.path.chmod(0o600)
            self._listener.listen(2)
            self._listener.settimeout(0.2)
        except OSError:
            self._listener.close()
            raise ScreenError("CONTROL_UNAVAILABLE") from None
        self._thread = threading.Thread(target=self._accept, daemon=True, name="screen-act")
        self._thread.start()

    def handle(self, request: dict) -> dict:
        """One request's answer (also the unit under test)."""
        try:
            helper = act_request(request.get("action"), request.get("params"))
        except ScreenError:
            return {"ok": False, "error": "INVALID_PARAMS"}
        with self._lock:
            if not self._allowed():
                return {"ok": False, "error": "HUMAN_HAS_CONTROL"}
            try:
                output = self._run(json.dumps(helper))
            except Exception:
                output = b""
        if output.strip() != b"ok":
            return {"ok": False, "error": "SCREEN_ACT_FAILED"}
        return {"ok": True}

    def _accept(self) -> None:
        while not self._stopping.is_set():
            try:
                connection, _ = self._listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            with connection:
                try:
                    connection.settimeout(15)
                    _send(connection, self.handle(_read(connection)))
                except (OSError, ScreenError, ValueError):
                    continue

    def close(self) -> None:
        self._stopping.set()
        self._listener.close()
        self._thread.join(timeout=2)
        self.path.unlink(missing_ok=True)


def request_act(path: Path, action: str, params: dict, *, timeout: float = 15) -> None:
    """Gateway side: do one desktop action, or ScreenError."""
    act_request(action, params)  # refuse before touching the socket
    if not path.exists():
        # A screen started before this service cannot act on its desktop.
        raise ScreenError("SCREEN_ACT_UNAVAILABLE")
    with socket.socket(socket.AF_UNIX) as connection:
        connection.settimeout(timeout)
        try:
            connection.connect(str(path))
            _send(connection, {"action": action, "params": params})
            answer = _read(connection)
        except (OSError, ValueError):
            raise ScreenError("SCREEN_ACT_FAILED") from None
    if answer.get("ok") is not True:
        error = answer.get("error")
        raise ScreenError(error if error in {"INVALID_PARAMS", "HUMAN_HAS_CONTROL"} else "SCREEN_ACT_FAILED")
