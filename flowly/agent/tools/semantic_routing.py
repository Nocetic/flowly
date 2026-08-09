"""Local semantic selection for progressively disclosed external tools.

This subsystem is intentionally independent from conversation recall.  It
indexes only static tool metadata, never imports the memory package, never
uses a remote embedding API, and never persists user queries.  If the local
encoder or the derived index is unavailable, callers receive an explicit
fallback decision and the existing lexical catalog remains authoritative.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
import sqlite3
import struct
import threading
import time
import unicodedata
from collections import OrderedDict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from loguru import logger

from flowly.agent.tools.routing import infer_toolset

_DOCUMENT_FORMAT_VERSION = "tool-document-v2"
_INDEX_SCHEMA_VERSION = 1
_TOKEN_RE = re.compile(r"[\w]+", re.UNICODE)

# Immutable model identity.  The model repository is fetched at this exact
# revision and the selected ONNX file is verified before inference.  We use a
# private registration alias so a future FastEmbed release cannot silently
# replace the model configuration behind the public repository name.
DEFAULT_MODEL_REPOSITORY = "intfloat/multilingual-e5-small"
DEFAULT_MODEL_REVISION = "614241f622f53c4eeff9890bdc4f31cfecc418b3"
DEFAULT_MODEL_FILE = "onnx/model_O4.onnx"
DEFAULT_MODEL_FILE_SIZE = 235_052_531
DEFAULT_MODEL_FILE_SHA256 = "4654c156f3e4171abc9c716cdb771bf9116455d15ac1aab364aeeede0e3205b0"
DEFAULT_MODEL_MANIFEST: dict[str, tuple[int, str]] = {
    "config.json": (
        655,
        "69137736cab8b8903a07fe8afaafdda25aac55415a12a55d1bffa9f581abf959",
    ),
    "sentencepiece.bpe.model": (
        5_069_051,
        "cfc8146abe2a0488e9e2a0c56de7952f7c11ab059eca145a0a727afce0db2865",
    ),
    "special_tokens_map.json": (
        167,
        "d05497f1da52c5e09554c0cd874037a083e1dc1b9cfd48034d1c717f1afc07a7",
    ),
    "tokenizer.json": (
        17_082_730,
        "0b44a9d7b51c3c62626640cda0e2c2f70fdacdc25bbbd68038369d14ebdf4c39",
    ),
    "tokenizer_config.json": (
        443,
        "a1d6bc8734a6f635dc158508bef000f8e2e5a759c7d92f984b2c86e5ff53425b",
    ),
    DEFAULT_MODEL_FILE: (DEFAULT_MODEL_FILE_SIZE, DEFAULT_MODEL_FILE_SHA256),
}
DEFAULT_ENCODER_ID = (
    "multilingual-e5-small-o4@"
    f"{DEFAULT_MODEL_REVISION}:{DEFAULT_MODEL_FILE_SHA256[:16]}"
)


def _packaged_model_dir() -> Path:
    """Return the stable package-data contract used by desktop bundles."""
    return Path(__file__).resolve().parents[2] / "assets" / "tool-routing-model"


class EmbeddingEncoder(Protocol):
    """Minimal local encoder contract used by the derived tool index."""

    encoder_id: str

    def embed_documents(self, texts: Iterable[str]) -> Sequence[Sequence[float]]:
        """Embed static tool documents."""

    def embed_query(self, text: str) -> Sequence[float]:
        """Embed one ephemeral user intent."""


@dataclass(frozen=True)
class SemanticToolDocument:
    """One stable, non-secret tool document ready for local embedding."""

    name: str
    source: str
    toolset: str
    promotable: bool
    text: str
    content_hash: str
    definition: dict[str, Any]


@dataclass(frozen=True)
class SemanticRoutingSnapshot:
    """Content-addressed external catalog snapshot."""

    generation: int
    fingerprint: str
    documents: Mapping[str, SemanticToolDocument]

    @property
    def promotable_names(self) -> frozenset[str]:
        """Names eligible for direct external-schema promotion."""
        return frozenset(
            name for name, document in self.documents.items()
            if document.promotable
        )


@dataclass(frozen=True)
class SemanticIndexSync:
    """Observable result of one incremental index synchronization."""

    total: int
    embedded: int
    unchanged: int
    removed: int


@dataclass(frozen=True)
class IndexedToolVector:
    """A normalized vector loaded from the local derived index."""

    name: str
    source: str
    toolset: str
    content_hash: str
    vector: tuple[float, ...]


@dataclass(frozen=True)
class SemanticRouteDecision:
    """Host-side selection result.

    ``promotions is None`` means semantic routing was unavailable and the
    existing deterministic router should decide.  An empty tuple is a valid,
    authoritative "no confident semantic match" result.
    """

    promotions: tuple[str, ...] | None
    mode: str
    state: str
    source: str = ""
    score: float = 0.0
    margin: float = 0.0
    latency_ms: float = 0.0
    public_reason: str = ""


def _fold_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", str(value or "").casefold())
    folded = "".join(char for char in normalized if not unicodedata.combining(char))
    return folded.replace("ı", "i")


def _normalized_label(value: str, *, limit: int = 100) -> str:
    folded = _fold_text(value)
    normalized = re.sub(r"[^a-z0-9._:-]+", "-", folded).strip("-._:")
    return normalized[:limit]


def _function(definition: Mapping[str, Any]) -> Mapping[str, Any]:
    value = definition.get("function")
    return value if isinstance(value, Mapping) else {}


def _definition_name(definition: Mapping[str, Any]) -> str:
    return str(_function(definition).get("name") or "").strip()


def _definition_source(name: str, declared: Mapping[str, str]) -> str:
    explicit = _normalized_label(str(declared.get(name) or ""))
    if explicit:
        return explicit
    parts = _fold_text(name).split("_")
    if len(parts) >= 3 and parts[0] == "mcp":
        return _normalized_label(parts[1]) or "mcp"
    return _normalized_label(infer_toolset(name)) or "external"


def _schema_text(definition: Mapping[str, Any], *, max_chars: int = 3_000) -> str:
    """Render only useful static schema metadata into a bounded document."""
    function = _function(definition)
    name = str(function.get("name") or "").strip()
    description = " ".join(str(function.get("description") or "").split())
    parameters = function.get("parameters")
    properties = (
        parameters.get("properties")
        if isinstance(parameters, Mapping)
        else None
    )
    rendered_parameters: list[str] = []
    if isinstance(properties, Mapping):
        for parameter_name, raw_schema in properties.items():
            schema = raw_schema if isinstance(raw_schema, Mapping) else {}
            parameter_type = schema.get("type") or "value"
            parameter_description = " ".join(
                str(schema.get("description") or "").split()
            )
            rendered_parameters.append(
                f"{parameter_name} ({parameter_type}): {parameter_description}".rstrip()
            )
    text = (
        f"Tool: {name}. Capability: {description}. Inputs: "
        + "; ".join(rendered_parameters)
    )
    return text[:max_chars]


def _document_text(
    definition: Mapping[str, Any],
    *,
    source: str,
    toolset: str,
) -> str:
    return (
        f"Provider: {source}. Tool group: {toolset}. "
        f"{_schema_text(definition)}"
    )


def _content_hash(
    definition: Mapping[str, Any],
    *,
    source: str,
    toolset: str,
    promotable: bool,
) -> str:
    payload = {
        "format": _DOCUMENT_FORMAT_VERSION,
        "source": source,
        "toolset": toolset,
        "promotable": promotable,
        "definition": definition,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def build_semantic_snapshot(
    definitions: Iterable[Mapping[str, Any]],
    *,
    toolsets: Mapping[str, str] | None = None,
    sources: Mapping[str, str] | None = None,
    deferred_toolsets: Iterable[str] = ("mcp", "extensions"),
    always_visible_tools: Iterable[str] = (),
    generation: int = 0,
) -> SemanticRoutingSnapshot:
    """Build a deterministic routing universe from static tool metadata.

    Deferred MCP/plugin tools are marked as promotable. Eager/core tools are
    deliberately indexed as non-promotable competitors: if a request is a
    better semantic match for a local/core capability, the router declines to
    expose an unrelated external schema. This changes neither tool visibility
    nor execution policy; the live routed surface remains authoritative.
    """
    declared_toolsets = {
        str(name): str(toolset).strip().lower()
        for name, toolset in (toolsets or {}).items()
    }
    declared_sources = {
        str(name): str(source)
        for name, source in (sources or {}).items()
    }
    deferred = {
        str(value).strip().lower()
        for value in deferred_toolsets
        if str(value).strip()
    }
    always = {str(value).strip() for value in always_visible_tools}
    documents: dict[str, SemanticToolDocument] = {}
    for raw_definition in definitions:
        definition = dict(raw_definition)
        name = _definition_name(definition)
        if not name:
            continue
        toolset = declared_toolsets.get(name, infer_toolset(name)).strip().lower()
        promotable = toolset in deferred and name not in always
        source = _definition_source(name, declared_sources)
        documents[name] = SemanticToolDocument(
            name=name,
            source=source,
            toolset=toolset,
            promotable=promotable,
            text=_document_text(definition, source=source, toolset=toolset),
            content_hash=_content_hash(
                definition,
                source=source,
                toolset=toolset,
                promotable=promotable,
            ),
            definition=definition,
        )
    fingerprint_input = "\n".join(
        f"{name}:{document.content_hash}"
        for name, document in sorted(documents.items())
    )
    fingerprint = hashlib.sha256(fingerprint_input.encode("utf-8")).hexdigest()
    return SemanticRoutingSnapshot(
        generation=max(0, int(generation)),
        fingerprint=fingerprint,
        documents=documents,
    )


def _normalize_vector(values: Sequence[float]) -> tuple[float, ...]:
    vector = tuple(float(value) for value in values)
    if not vector or not all(math.isfinite(value) for value in vector):
        raise ValueError("embedding vector is empty or non-finite")
    norm = math.sqrt(sum(value * value for value in vector))
    if not math.isfinite(norm) or norm <= 1e-12:
        raise ValueError("embedding vector has zero norm")
    return tuple(value / norm for value in vector)


def _pack_vector(vector: Sequence[float]) -> bytes:
    return struct.pack(f"<{len(vector)}f", *vector)


def _unpack_vector(blob: bytes, dimension: int) -> tuple[float, ...]:
    if dimension <= 0 or len(blob) != dimension * 4:
        raise ValueError("stored embedding has an invalid dimension")
    return _normalize_vector(struct.unpack(f"<{dimension}f", blob))


class SemanticToolIndex:
    """Profile-scoped SQLite store for static tool vectors."""

    _SCHEMA = """
    CREATE TABLE IF NOT EXISTS metadata (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS tool_vectors (
        encoder_id TEXT NOT NULL,
        tool_name TEXT NOT NULL,
        source TEXT NOT NULL,
        toolset TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        dimension INTEGER NOT NULL,
        vector BLOB NOT NULL,
        updated_at REAL NOT NULL,
        PRIMARY KEY (encoder_id, tool_name)
    );
    CREATE INDEX IF NOT EXISTS idx_tool_vectors_encoder_source
        ON tool_vectors(encoder_id, source);
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._write_lock = threading.RLock()
        self._ensure_schema_with_recovery()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.path), timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        return connection

    def _ensure_schema(self) -> None:
        with self._connect() as connection:
            connection.executescript(self._SCHEMA)
            connection.execute(
                "INSERT INTO metadata(key, value) VALUES('schema_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(_INDEX_SCHEMA_VERSION),),
            )
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def _quarantine(self) -> Path | None:
        if not self.path.exists():
            return None
        suffix = f".corrupt-{time.time_ns()}"
        target = self.path.with_name(self.path.name + suffix)
        os.replace(self.path, target)
        for extension in ("-wal", "-shm"):
            sidecar = Path(str(self.path) + extension)
            if sidecar.exists():
                try:
                    os.replace(sidecar, Path(str(target) + extension))
                except OSError:
                    pass
        logger.warning("Semantic tool index was corrupt; quarantined at {}", target)
        return target

    def _ensure_schema_with_recovery(self) -> None:
        try:
            self._ensure_schema()
        except (sqlite3.DatabaseError, sqlite3.OperationalError):
            self._quarantine()
            self._ensure_schema()

    def _current_rows(self, encoder_id: str) -> dict[str, sqlite3.Row]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT tool_name, source, toolset, content_hash, dimension, vector "
                "FROM tool_vectors WHERE encoder_id = ?",
                (encoder_id,),
            ).fetchall()
        return {str(row["tool_name"]): row for row in rows}

    def sync(
        self,
        snapshot: SemanticRoutingSnapshot,
        encoder: EmbeddingEncoder,
    ) -> SemanticIndexSync:
        """Embed one content-addressed delta and commit it atomically."""
        with self._write_lock:
            try:
                current = self._current_rows(encoder.encoder_id)
            except (sqlite3.DatabaseError, sqlite3.OperationalError):
                self._quarantine()
                self._ensure_schema()
                current = {}

            changed = [
                document
                for name, document in sorted(snapshot.documents.items())
                if name not in current
                or str(current[name]["content_hash"]) != document.content_hash
                or str(current[name]["source"]) != document.source
                or str(current[name]["toolset"]) != document.toolset
            ]
            stale = sorted(set(current) - set(snapshot.documents))
            vectors: list[tuple[float, ...]] = []
            if changed:
                embedded = list(encoder.embed_documents(
                    document.text for document in changed
                ))
                if len(embedded) != len(changed):
                    raise ValueError("encoder returned a different vector count")
                vectors = [_normalize_vector(vector) for vector in embedded]
                dimensions = {len(vector) for vector in vectors}
                if len(dimensions) != 1:
                    raise ValueError("encoder returned inconsistent dimensions")

            now = time.time()
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    for document, vector in zip(changed, vectors):
                        connection.execute(
                            "INSERT INTO tool_vectors("
                            "encoder_id, tool_name, source, toolset, content_hash, "
                            "dimension, vector, updated_at"
                            ") VALUES(?, ?, ?, ?, ?, ?, ?, ?) "
                            "ON CONFLICT(encoder_id, tool_name) DO UPDATE SET "
                            "source=excluded.source, toolset=excluded.toolset, "
                            "content_hash=excluded.content_hash, "
                            "dimension=excluded.dimension, vector=excluded.vector, "
                            "updated_at=excluded.updated_at",
                            (
                                encoder.encoder_id,
                                document.name,
                                document.source,
                                document.toolset,
                                document.content_hash,
                                len(vector),
                                _pack_vector(vector),
                                now,
                            ),
                        )
                    if stale:
                        placeholders = ",".join("?" for _ in stale)
                        connection.execute(
                            "DELETE FROM tool_vectors WHERE encoder_id = ? "
                            f"AND tool_name IN ({placeholders})",
                            (encoder.encoder_id, *stale),
                        )
                    connection.commit()
                except Exception:
                    connection.rollback()
                    raise

            return SemanticIndexSync(
                total=len(snapshot.documents),
                embedded=len(changed),
                unchanged=len(snapshot.documents) - len(changed),
                removed=len(stale),
            )

    def load(self, encoder_id: str) -> dict[str, IndexedToolVector]:
        """Load and validate one encoder namespace."""
        rows = self._current_rows(encoder_id)
        result: dict[str, IndexedToolVector] = {}
        for name, row in rows.items():
            result[name] = IndexedToolVector(
                name=name,
                source=str(row["source"]),
                toolset=str(row["toolset"]),
                content_hash=str(row["content_hash"]),
                vector=_unpack_vector(bytes(row["vector"]), int(row["dimension"])),
            )
        return result

    def reset_encoder(self, encoder_id: str) -> None:
        """Drop one derived encoder namespace so it can be rebuilt safely."""
        with self._write_lock:
            try:
                with self._connect() as connection:
                    connection.execute(
                        "DELETE FROM tool_vectors WHERE encoder_id = ?",
                        (encoder_id,),
                    )
            except (sqlite3.DatabaseError, sqlite3.OperationalError):
                self._quarantine()
                self._ensure_schema()


_FASTEMBED_REGISTRATION_LOCK = threading.Lock()
_FASTEMBED_REGISTERED = False


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class FastEmbedLocalEncoder:
    """Pinned multilingual ONNX encoder used only for tool routing."""

    encoder_id = DEFAULT_ENCODER_ID

    def __init__(
        self,
        *,
        cache_dir: Path,
        allow_download: bool,
        threads: int | None = None,
    ) -> None:
        cache_dir = Path(cache_dir)
        cache_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        model_dir = self._resolve_model(cache_dir, allow_download=allow_download)

        from fastembed import TextEmbedding
        from fastembed.common.model_description import ModelSource, PoolingType

        global _FASTEMBED_REGISTERED
        alias = "flowly/multilingual-e5-small-o4-v1"
        with _FASTEMBED_REGISTRATION_LOCK:
            if not _FASTEMBED_REGISTERED:
                registered = {
                    str(model.get("model") or "").casefold()
                    for model in TextEmbedding.list_supported_models()
                }
                if alias.casefold() not in registered:
                    TextEmbedding.add_custom_model(
                        model=alias,
                        pooling=PoolingType.MEAN,
                        normalization=True,
                        sources=ModelSource(hf=DEFAULT_MODEL_REPOSITORY),
                        dim=384,
                        model_file=DEFAULT_MODEL_FILE,
                        description="Pinned multilingual retrieval model for tool routing.",
                        license="mit",
                        size_in_gb=0.24,
                    )
                _FASTEMBED_REGISTERED = True

        self._model = TextEmbedding(
            model_name=alias,
            cache_dir=str(cache_dir),
            threads=threads or max(1, min(4, os.cpu_count() or 1)),
            lazy_load=False,
            specific_model_path=str(model_dir),
            local_files_only=True,
        )

    @staticmethod
    def _resolve_model(cache_dir: Path, *, allow_download: bool) -> Path:
        from huggingface_hub import snapshot_download

        bundled = str(os.environ.get("FLOWLY_SEMANTIC_MODEL_DIR") or "").strip()
        if bundled:
            model_dir = Path(bundled).expanduser()
        elif (_packaged_model_dir() / DEFAULT_MODEL_FILE).is_file():
            model_dir = _packaged_model_dir()
        else:
            model_dir = Path(snapshot_download(
                repo_id=DEFAULT_MODEL_REPOSITORY,
                revision=DEFAULT_MODEL_REVISION,
                cache_dir=str(cache_dir),
                local_files_only=not allow_download,
                allow_patterns=sorted(DEFAULT_MODEL_MANIFEST),
            ))
        # This work runs on the background warm-up thread. Hashing on every
        # process start is intentional: a writable cache marker alone cannot
        # prove that immutable weights or tokenizer assets were not replaced.
        for relative_path, (expected_size, expected_sha256) in (
            DEFAULT_MODEL_MANIFEST.items()
        ):
            asset = model_dir / relative_path
            if not asset.is_file():
                raise FileNotFoundError("local semantic routing model is unavailable")
            if asset.stat().st_size != expected_size:
                raise ValueError("local semantic routing model has an unexpected size")
            if _sha256_file(asset) != expected_sha256:
                raise ValueError("local semantic routing model checksum failed")
        return model_dir

    def embed_documents(self, texts: Iterable[str]) -> list[Sequence[float]]:
        passages = [f"passage: {text}" for text in texts]
        return list(self._model.embed(passages, batch_size=32))

    def embed_query(self, text: str) -> Sequence[float]:
        result = list(self._model.embed([f"query: {text}"], batch_size=1))
        if len(result) != 1:
            raise ValueError("local encoder did not return one query vector")
        return result[0]


class SemanticToolRouter:
    """Background-indexed, confidence-gated semantic tool selector."""

    def __init__(
        self,
        *,
        state_dir: Path,
        model_cache_dir: Path | None = None,
        encoder_factory: Callable[[], EmbeddingEncoder] | None = None,
        auto_download: bool = True,
        source_min_score: float = 0.79,
        source_min_margin: float = 0.025,
        tool_min_score: float = 0.77,
        tool_score_window: float = 0.035,
        workflow_max_tools: int = 4,
        query_cache_size: int = 256,
    ) -> None:
        root = Path(state_dir) / "tool-routing"
        self.index_path = root / "index.sqlite3"
        self._model_cache = Path(model_cache_dir) if model_cache_dir else root / "models"
        self._index = SemanticToolIndex(self.index_path)
        self._auto_download = bool(auto_download)
        self._encoder_factory = encoder_factory or (
            lambda: FastEmbedLocalEncoder(
                cache_dir=self._model_cache,
                allow_download=self._auto_download,
            )
        )
        self._source_min_score = float(source_min_score)
        self._source_min_margin = float(source_min_margin)
        self._tool_min_score = float(tool_min_score)
        self._tool_score_window = max(0.0, float(tool_score_window))
        self._workflow_max_tools = max(1, int(workflow_max_tools))
        self._query_cache_size = max(1, int(query_cache_size))

        self._lock = threading.RLock()
        self._encoder_init_lock = threading.Lock()
        self._inference_lock = threading.Lock()
        self._encoder: EmbeddingEncoder | None = None
        self._vectors: Mapping[str, IndexedToolVector] = {}
        self._vector_matrix: Any | None = None
        self._vector_rows: Mapping[str, int] = {}
        self._ready_fingerprint = ""
        self._ready_generation = -1
        self._pending: SemanticRoutingSnapshot | None = None
        self._active_fingerprint = ""
        self._worker: threading.Thread | None = None
        self._state = "cold"
        self._closed = False
        self._failures = 0
        self._retry_after = 0.0
        self._last_error = ""
        self._query_cache: OrderedDict[str, tuple[float, ...]] = OrderedDict()

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    @property
    def last_error(self) -> str:
        with self._lock:
            return self._last_error

    def _get_encoder(self) -> EmbeddingEncoder:
        with self._lock:
            if self._encoder is not None:
                return self._encoder
        with self._encoder_init_lock:
            with self._lock:
                if self._encoder is not None:
                    return self._encoder
            encoder = self._encoder_factory()
            with self._lock:
                if self._encoder is None:
                    self._encoder = encoder
                return self._encoder

    def prepare_sync(
        self,
        snapshot: SemanticRoutingSnapshot,
    ) -> SemanticIndexSync | None:
        """Synchronously prepare an index; used by workers and diagnostics."""
        if self._closed:
            return None
        try:
            encoder = self._get_encoder()
            sync = self._index.sync(snapshot, encoder)
            try:
                vectors = self._index.load(encoder.encoder_id)
            except (ValueError, sqlite3.DatabaseError, sqlite3.OperationalError):
                # The index is derived and contains no user data. A malformed
                # vector must self-heal instead of pinning every future turn to
                # fallback until the user manually removes a database file.
                logger.warning("Semantic tool vectors were invalid; rebuilding index")
                self._index.reset_encoder(encoder.encoder_id)
                sync = self._index.sync(snapshot, encoder)
                vectors = self._index.load(encoder.encoder_id)
            expected = {
                name: document.content_hash
                for name, document in snapshot.documents.items()
            }
            ready = {
                name: vector
                for name, vector in vectors.items()
                if expected.get(name) == vector.content_hash
            }
            if set(ready) != set(expected):
                raise ValueError("semantic index did not produce a complete snapshot")
            ordered_names = tuple(sorted(ready))
            try:
                import numpy as np

                vector_matrix: Any | None = np.asarray(
                    [ready[name].vector for name in ordered_names],
                    dtype=np.float32,
                )
                vector_matrix.setflags(write=False)
            except Exception:  # noqa: BLE001 - scalar fallback stays correct
                vector_matrix = None
            with self._lock:
                self._vectors = ready
                self._vector_matrix = vector_matrix
                self._vector_rows = {
                    name: index for index, name in enumerate(ordered_names)
                }
                self._ready_fingerprint = snapshot.fingerprint
                self._ready_generation = snapshot.generation
                self._state = "ready"
                self._failures = 0
                self._retry_after = 0.0
                self._last_error = ""
            logger.info(
                "Semantic tool index ready: generation={}, total={}, embedded={}, "
                "removed={}, encoder={}",
                snapshot.generation,
                sync.total,
                sync.embedded,
                sync.removed,
                encoder.encoder_id,
            )
            return sync
        except Exception as exc:  # noqa: BLE001 - this subsystem must fail open
            with self._lock:
                self._failures += 1
                cooldown = min(3600.0, 15.0 * (2 ** min(self._failures - 1, 8)))
                self._retry_after = time.monotonic() + cooldown
                self._state = "unavailable"
                self._last_error = f"{type(exc).__name__}: {exc}"
            log = logger.warning if self._auto_download else logger.debug
            log(
                "Semantic tool routing unavailable; lexical discovery remains active: {}",
                type(exc).__name__,
            )
            return None

    def prepare(self, snapshot: SemanticRoutingSnapshot) -> bool:
        """Coalesce a snapshot onto one daemon indexing worker."""
        with self._lock:
            if self._closed or self._ready_fingerprint == snapshot.fingerprint:
                return self._ready_fingerprint == snapshot.fingerprint
            if snapshot.generation < self._ready_generation:
                return False
            if time.monotonic() < self._retry_after:
                return False
            # Registry listeners and the explicit MCP discovery completion
            # hook may observe the same generation almost simultaneously. Do
            # not queue an identical snapshot behind the one already being
            # indexed; doing so wastes CPU and can initialize the encoder
            # twice during first-run startup.
            if self._active_fingerprint == snapshot.fingerprint:
                return False
            if (
                self._pending is not None
                and self._pending.fingerprint == snapshot.fingerprint
            ):
                return False
            if (
                self._pending is None
                or snapshot.generation >= self._pending.generation
            ):
                self._pending = snapshot
            if self._worker is not None and self._worker.is_alive():
                return False
            self._state = "indexing"
            self._worker = threading.Thread(
                target=self._worker_main,
                name="flowly-tool-index",
                daemon=True,
            )
            self._worker.start()
            return False

    def _worker_main(self) -> None:
        while True:
            with self._lock:
                if self._closed:
                    self._worker = None
                    return
                snapshot = self._pending
                self._pending = None
                self._active_fingerprint = (
                    snapshot.fingerprint if snapshot is not None else ""
                )
            if snapshot is None:
                with self._lock:
                    self._worker = None
                    self._active_fingerprint = ""
                return
            try:
                self.prepare_sync(snapshot)
            finally:
                with self._lock:
                    if self._active_fingerprint == snapshot.fingerprint:
                        self._active_fingerprint = ""

    def _query_vector(self, intent: str) -> tuple[float, ...]:
        key = hashlib.sha256(intent.encode("utf-8")).hexdigest()
        with self._lock:
            cached = self._query_cache.get(key)
            if cached is not None:
                self._query_cache.move_to_end(key)
                return cached
        encoder = self._get_encoder()
        with self._inference_lock:
            # Another selection may have populated the cache while this call
            # waited for the single ONNX inference slot.
            with self._lock:
                cached = self._query_cache.get(key)
                if cached is not None:
                    self._query_cache.move_to_end(key)
                    return cached
            vector = _normalize_vector(encoder.embed_query(intent))
        with self._lock:
            self._query_cache[key] = vector
            self._query_cache.move_to_end(key)
            while len(self._query_cache) > self._query_cache_size:
                self._query_cache.popitem(last=False)
        return vector

    @staticmethod
    def _dot(left: Sequence[float], right: Sequence[float]) -> float:
        if len(left) != len(right):
            return -1.0
        return sum(a * b for a, b in zip(left, right))

    def _ready_decision(
        self,
        intent: str,
        snapshot: SemanticRoutingSnapshot,
        *,
        allowed_names: frozenset[str],
        max_tools: int,
    ) -> SemanticRouteDecision:
        started = time.perf_counter()
        allowed = sorted(set(allowed_names).intersection(snapshot.documents))
        if not str(intent or "").strip() or not allowed or max_tools <= 0:
            return SemanticRouteDecision(
                promotions=(),
                mode="no_match",
                state="ready",
            )
        with self._lock:
            if self._ready_fingerprint != snapshot.fingerprint:
                return SemanticRouteDecision(
                    promotions=None,
                    mode="fallback",
                    state=self._state,
                    public_reason="Local semantic index is still preparing.",
                )
            vectors = dict(self._vectors)
            vector_matrix = self._vector_matrix
            vector_rows = dict(self._vector_rows)
        eligible = {
            name: vectors[name]
            for name in allowed
            if name in vectors
            and vectors[name].content_hash == snapshot.documents[name].content_hash
        }
        if not eligible:
            return SemanticRouteDecision(
                promotions=None,
                mode="fallback",
                state="stale",
                public_reason="Local semantic index is not current.",
            )

        query = self._query_vector(str(intent))
        scores: dict[str, float]
        if vector_matrix is not None:
            import numpy as np

            query_array = np.asarray(query, dtype=np.float32)
            if vector_matrix.shape[1] != query_array.shape[0]:
                raise ValueError("query and document embedding dimensions differ")
            all_scores = vector_matrix @ query_array
            scores = {
                name: float(all_scores[vector_rows[name]])
                for name in eligible
            }
        else:
            scores = {
                name: self._dot(query, item.vector)
                for name, item in eligible.items()
            }
        source_scores: dict[str, float] = {}
        source_tools: dict[str, list[str]] = {}
        for name, score in scores.items():
            source = eligible[name].source
            source_scores[source] = max(source_scores.get(source, -1.0), score)
            source_tools.setdefault(source, []).append(name)
        ranked_sources = sorted(
            source_scores.items(),
            key=lambda item: (-item[1], item[0]),
        )
        best_source, best_score = ranked_sources[0]
        second_score = ranked_sources[1][1] if len(ranked_sources) > 1 else -1.0
        margin = best_score - second_score
        latency_ms = (time.perf_counter() - started) * 1000
        if best_score < self._source_min_score:
            return SemanticRouteDecision(
                promotions=(),
                mode="no_match",
                state="ready",
                source=best_source,
                score=best_score,
                margin=margin,
                latency_ms=latency_ms,
            )
        if len(ranked_sources) > 1 and margin < self._source_min_margin:
            return SemanticRouteDecision(
                promotions=(),
                mode="ambiguous",
                state="ready",
                source=best_source,
                score=best_score,
                margin=margin,
                latency_ms=latency_ms,
            )

        # A core/eager source may win the semantic competition, but it can
        # never be promoted by this subsystem. It is already present on the
        # model-facing surface (or intentionally hidden by live policy).
        promotable_source_tools = [
            name
            for name in source_tools[best_source]
            if snapshot.documents[name].promotable
        ]
        if not promotable_source_tools:
            return SemanticRouteDecision(
                promotions=(),
                mode="no_match",
                state="ready",
                source=best_source,
                score=best_score,
                margin=margin,
                latency_ms=latency_ms,
            )

        within_source = sorted(
            promotable_source_tools,
            key=lambda name: (-scores[name], name),
        )
        bounded_max = max(0, int(max_tools))
        if len(within_source) <= min(self._workflow_max_tools, bounded_max):
            promoted = tuple(sorted(within_source))
        else:
            top_tool_score = scores[within_source[0]]
            cutoff = max(self._tool_min_score, top_tool_score - self._tool_score_window)
            promoted = tuple(
                name
                for name in within_source
                if scores[name] >= cutoff
            )[:bounded_max]
        if not promoted:
            return SemanticRouteDecision(
                promotions=(),
                mode="no_match",
                state="ready",
                source=best_source,
                score=best_score,
                margin=margin,
                latency_ms=latency_ms,
            )
        return SemanticRouteDecision(
            promotions=promoted,
            mode="semantic",
            state="ready",
            source=best_source,
            score=best_score,
            margin=margin,
            latency_ms=latency_ms,
        )

    def select_sync(
        self,
        intent: str,
        snapshot: SemanticRoutingSnapshot,
        *,
        allowed_names: frozenset[str],
        max_tools: int = 4,
    ) -> SemanticRouteDecision:
        """Prepare if necessary and select; intended for tests/diagnostics."""
        if not set(allowed_names).intersection(snapshot.promotable_names):
            return SemanticRouteDecision(
                promotions=(),
                mode="no_match",
                state="idle",
            )
        with self._lock:
            ready = self._ready_fingerprint == snapshot.fingerprint
        if not ready and self.prepare_sync(snapshot) is None:
            return SemanticRouteDecision(
                promotions=None,
                mode="fallback",
                state=self.state,
                public_reason="Local semantic routing is unavailable.",
            )
        try:
            return self._ready_decision(
                intent,
                snapshot,
                allowed_names=allowed_names,
                max_tools=max_tools,
            )
        except Exception as exc:  # noqa: BLE001 - routing cannot break a turn
            logger.warning("Semantic selection failed; using lexical fallback: {}", type(exc).__name__)
            return SemanticRouteDecision(
                promotions=None,
                mode="fallback",
                state="unavailable",
                public_reason="Local semantic routing is unavailable.",
            )

    async def select(
        self,
        intent: str,
        snapshot: SemanticRoutingSnapshot,
        *,
        allowed_names: frozenset[str],
        max_tools: int = 4,
    ) -> SemanticRouteDecision:
        """Return immediately with fallback while a changed index prepares."""
        if not set(allowed_names).intersection(snapshot.promotable_names):
            return SemanticRouteDecision(
                promotions=(),
                mode="no_match",
                state="idle",
            )
        if not self.prepare(snapshot):
            with self._lock:
                ready = self._ready_fingerprint == snapshot.fingerprint
                state = self._state
            if not ready:
                return SemanticRouteDecision(
                    promotions=None,
                    mode="fallback",
                    state=state,
                    public_reason="Local semantic index is preparing.",
                )
        try:
            return await asyncio.to_thread(
                self._ready_decision,
                intent,
                snapshot,
                allowed_names=allowed_names,
                max_tools=max_tools,
            )
        except Exception as exc:  # noqa: BLE001 - routing cannot break a turn
            logger.warning("Semantic selection failed; using lexical fallback: {}", type(exc).__name__)
            return SemanticRouteDecision(
                promotions=None,
                mode="fallback",
                state="unavailable",
                public_reason="Local semantic routing is unavailable.",
            )

    def close(self) -> None:
        """Stop accepting background work; an in-flight daemon may finish."""
        with self._lock:
            self._closed = True
            self._pending = None
