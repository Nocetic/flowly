"""Cold token estimation must never wait for an encoding CDN on the event loop."""

import os
import subprocess
import sys
from pathlib import Path

import pytest


def test_bundled_encoder_matches_upstream_definition_without_http(monkeypatch):
    import base64
    import gzip

    import tiktoken
    import tiktoken_ext.openai_public as upstream

    from flowly.compaction.offline_encoding import load_encoding

    data = Path(__file__).resolve().parents[1] / "flowly/compaction/data/cl100k_base.tiktoken.gz"
    ranks = {base64.b64decode(token): int(rank)
             for token, rank in (line.split() for line in gzip.decompress(data.read_bytes()).splitlines())}
    # Only substitute the upstream data loader; keep its regex/special-token
    # definition independent, so upgrades cannot silently change our estimates.
    monkeypatch.setattr(upstream, "load_tiktoken_bpe", lambda *a, **kw: ranks)
    reference = tiktoken.Encoding(**upstream.cl100k_base())
    bundled = load_encoding()
    for value in ["Hello, world!", "şğıİçöü 日本語 👋🏽", "1234567890",
                  "\t  \n\r\n  ", "def f(x):\n    return x + 1\n",
                  '{"role":"user","content":"we\'ll test it"}',
                  "<|endoftext|><|fim_prefix|><|fim_middle|><|fim_suffix|><|endofprompt|>"]:
        assert bundled.encode(value, allowed_special="all") == reference.encode(value, allowed_special="all")
    with pytest.raises(ValueError):
        bundled.encode("<|endoftext|>")


def test_corrupt_bundled_data_fails_locally(monkeypatch, tmp_path):
    import gzip
    import socket

    from flowly.compaction import offline_encoding

    data = tmp_path / "data"
    data.mkdir()
    (data / "cl100k_base.tiktoken.gz").write_bytes(gzip.compress(b"invalid ranks"))
    monkeypatch.setattr(offline_encoding, "files", lambda _: tmp_path)

    def no_network(*args, **kwargs):
        pytest.fail("corrupt data must not trigger a network fallback")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    with pytest.raises(RuntimeError, match="Bundled tokenizer data is invalid"):
        offline_encoding.load_encoding()


def test_cold_estimator_works_without_network_or_writable_token_cache(tmp_path):
    cache = tmp_path / "not-a-directory"
    cache.write_text("no runtime cache writes")
    script = '''
import socket
def forbidden(*args, **kwargs):
    raise AssertionError("token estimation attempted a network connection")
socket.socket.connect = forbidden
from flowly.compaction.estimator import estimate_tokens, estimate_messages_tokens
assert estimate_tokens("Hello, world!") == 4
assert estimate_tokens("şğıİçöü 日本語 👋") > 0
assert estimate_messages_tokens([{"role":"user", "content":"Hello, world!"}]) == 10
from flowly.memory.chunker import _count_tokens, chunk_text
assert _count_tokens("Hello, world!") == 4  # existing fallback would return 3
assert [c.text for c in chunk_text("Hello, world!\\nGoodbye!", chunk_tokens=4, overlap_tokens=0)] == ["Hello, world!", "Goodbye!"]
print("offline cold estimation passed")
'''
    result = subprocess.run([sys.executable, "-c", script],
                            cwd=Path(__file__).resolve().parents[1],
                            env={**os.environ, "TIKTOKEN_CACHE_DIR": str(cache)},
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert "offline cold estimation passed" in result.stdout
    assert cache.read_text() == "no runtime cache writes"
