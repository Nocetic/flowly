# Offline cl100k_base data

`cl100k_base.tiktoken.gz` is a deterministic gzip (mtime zero) of the public
tiktoken merge ranks from:

https://openaipublic.blob.core.windows.net/encodings/cl100k_base.tiktoken

Uncompressed size: 1,681,126 bytes. SHA-256:
`223921b76ee99bde995b7ff738513eef100fb51d18c93597a113bcffe865b2a7`.
This is also the expected hash in tiktoken's `openai_public.cl100k_base`
constructor. The definition in `../offline_encoding.py` matches that constructor
(tiktoken 0.12.0); `LICENSE.tiktoken` preserves the upstream MIT notice.

The estimator validates and loads this data directly. It never consults a user
token cache, downloads data, patches tiktoken globals, or replaces exact counts
with a heuristic. This prevents a cold encoding-cache HTTP fetch from blocking
the agent's event loop and screen/relay control RPCs. A corrupt/missing package
resource requires repair/reinstallation; it cannot trigger a runtime download.

The wheel/sdist package includes this directory. The CLI entry's Nuitka
package-data directive includes it in standalone artifacts too; target-platform
packaged acceptance remains a separate release gate. When updating the encoding,
update definition, verified data/hash, provenance and parity tests together.
