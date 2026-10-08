"""Frame latency through SharedRfbPeer (supervisor capture proxy) vs. a direct socket.

A synthetic RFB 3.8 server sends Raw 32bpp framebuffer updates carrying their
send time at 60 fps
the reader records arrival of each update's last byte.
"""

import json
import socket
import statistics
import struct
import threading
import time

from flowly.desktop_runtime.shared_rfb import SharedRfbPeer

W, H, FRAMES = 128, 96, 600
PIXELS = W * H * 4


def server(sock):
    sock.sendall(b"RFB 003.008\n")
    assert sock.recv(12) == b"RFB 003.008\n"
    sock.sendall(b"\x01\x01")
    sock.recv(1)
    sock.sendall(bytes(4))
    sock.recv(1)
    fmt = bytes([32, 24, 0, 1, 0, 255, 0, 255, 0, 255, 16, 8, 0, 0, 0, 0])
    sock.sendall(struct.pack("!HH", 1440, 900) + fmt + struct.pack("!I", 4) + b"test")
    sock.settimeout(0.05)
    try:  # client SetPixelFormat / SetEncodings
        while sock.recv(4096):
            pass
    except (socket.timeout, OSError):
        pass
    sock.settimeout(None)
    filler = bytes(PIXELS - 8)
    next_at = time.perf_counter()
    for _ in range(FRAMES):
        header = struct.pack("!BxH", 0, 1) + struct.pack("!HHHHi", 0, 0, W, H, 0)
        sock.sendall(header + time.perf_counter_ns().to_bytes(8, "big") + filler)
        next_at += 1 / 60
        time.sleep(max(0, next_at - time.perf_counter()))


def client(sock):
    def read(n):
        out = bytearray()
        while len(out) < n:
            chunk = sock.recv(n - len(out))
            if not chunk:
                raise EOFError
            out.extend(chunk)
        return bytes(out)

    assert read(12) == b"RFB 003.008\n"
    sock.sendall(b"RFB 003.008\n")
    read(2)
    sock.sendall(b"\x01")
    read(4)
    sock.sendall(b"\x01")
    init = read(24)
    read(int.from_bytes(init[20:24], "big"))
    fmt = bytes([32, 24, 0, 1, 0, 255, 0, 255, 0, 255, 16, 8, 0, 0, 0, 0])
    sock.sendall(b"\x00\x00\x00\x00" + fmt)
    encodings = [0, -312]
    sock.sendall(
        struct.pack("!BxH", 2, len(encodings)) + b"".join(struct.pack("!i", e) for e in encodings)
    )
    delays = []
    for _ in range(FRAMES):
        read(4)
        read(12)
        body = read(PIXELS)
        delays.append((time.perf_counter_ns() - int.from_bytes(body[:8], "big")) / 1e6)
    return delays


def run(shared):
    backend_server, backend = socket.socketpair()
    peer = None
    if shared:
        front_gateway, front_supervisor = socket.socketpair()
        peer = SharedRfbPeer(front_supervisor, backend)
        reader = front_gateway
    else:
        reader = backend
    threading.Thread(target=server, args=(backend_server,), daemon=True).start()
    delays = client(reader)
    if peer is not None:
        peer.join()
    q = statistics.quantiles(delays, n=100)
    return {
        "path": "supervisor-proxy" if shared else "direct",
        "frameKiB": PIXELS // 1024,
        "frames": len(delays),
        "p50": round(q[49], 2),
        "p95": round(q[94], 2),
        "p99": round(q[98], 2),
        "max": round(max(delays), 2),
    }


if __name__ == "__main__":
    for _ in range(2):
        for shared in (False, True):
            print(json.dumps(run(shared)))
