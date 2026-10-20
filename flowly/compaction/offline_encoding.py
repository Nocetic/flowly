"""The existing cl100k estimator, with verified local data and no runtime HTTP.

The encoding definition is from tiktoken (MIT); see data/LICENSE.tiktoken and
data/README.md. Do not route this through get_encoding: its plugin constructor
downloads an absent cache synchronously, including from the gateway event loop.
"""

import base64
import gzip
import hashlib
from importlib.resources import files

import tiktoken

_SHA256 = "223921b76ee99bde995b7ff738513eef100fb51d18c93597a113bcffe865b2a7"
_PATTERN = r"""'(?i:[sdmt]|ll|ve|re)|[^\r\n\p{L}\p{N}]?+\p{L}++|\p{N}{1,3}+| ?[^\s\p{L}\p{N}]++[\r\n]*+|\s++$|\s*[\r\n]|\s+(?!\S)|\s"""


def load_encoding() -> tiktoken.Encoding:
    resource = files("flowly.compaction").joinpath("data/cl100k_base.tiktoken.gz")
    with resource.open("rb") as source, gzip.GzipFile(fileobj=source) as compressed:
        raw = compressed.read(2_000_001)
    if hashlib.sha256(raw).hexdigest() != _SHA256:
        raise RuntimeError("Bundled tokenizer data is invalid; reinstall Flowly.")
    ranks = {base64.b64decode(token, validate=True): int(rank)
             for token, rank in (line.split() for line in raw.splitlines())}
    return tiktoken.Encoding(
        name="cl100k_base", pat_str=_PATTERN, mergeable_ranks=ranks,
        special_tokens={"<|endoftext|>": 100257, "<|fim_prefix|>": 100258,
                        "<|fim_middle|>": 100259, "<|fim_suffix|>": 100260,
                        "<|endofprompt|>": 100276},
    )
