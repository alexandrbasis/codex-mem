"""Bounded, local semantic indexing for Codex Mem.

The ordinary memory store stays dependency-free.  This module is the optional
semantic layer: it only imports FastEmbed after a caller asks to encode text,
and it never downloads a model while handling a search or a hook.  The one
networking entrypoint is :func:`prepare_model`.

Stored text remains untrusted evidence.  It is redacted again before it reaches
the embedding model, and results are previews supplied by :class:`Store`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextlib import contextmanager
import hashlib
import importlib.metadata
import importlib.util
import math
import os
from pathlib import Path
import tempfile
import threading
from typing import Any, Protocol, runtime_checkable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .privacy import redact_text
from .store import Store, StoreError, project_key


MODEL = "intfloat/multilingual-e5-base"
"""Pinned multilingual retrieval model, registered locally with FastEmbed."""

MODEL_REPOSITORY = "intfloat/multilingual-e5-base"
MODEL_REVISION = "d128750597153bb5987e10b1c3493a34e5a4502a"
MODEL_FILE = "model_qint8_avx512_vnni.onnx"
MODEL_SHA256 = "2523551878658b305550d8759443822dbfda9ed9c8012ef2c354ba2c5b9de503"
TOKENIZER_SHA256 = "62c24cdc13d4c9952d63718d6c9fa4c287974249e16b7ade6d5a85e7bbb75626"
DIMENSIONS = 768

# E5 was trained asymmetrically.  These literal ASCII prefixes are part of
# its documented input contract, including for non-English input.
QUERY_PREFIX = "query: "
PASSAGE_PREFIX = "passage: "

FASTEMBED_VERSION = "0.8.0"
ONNXRUNTIME_VERSION = "1.23.2"

MODEL_FILES = (
    MODEL_FILE,
    "config.json",
    "tokenizer.json",
    "tokenizer_config.json",
    "special_tokens_map.json",
)
_CHECKSUMS = {
    MODEL_FILE: MODEL_SHA256,
    "config.json": "9dab198f24c8c0879e481cf7822005d5ecbceedbacb390ffafa594e28d31bac4",
    "tokenizer.json": TOKENIZER_SHA256,
    "tokenizer_config.json": "efb5c0d09722e5fe59a462cd2a9976ee216d55b037597d997cd3fe833216da15",
    "special_tokens_map.json": "06e405a36dfe4b9604f484f6a1e619af1a7f7d09e34a8555eb0b77b66318067f",
}
_REMOTE_ARTIFACTS = {
    MODEL_FILE: f"onnx/{MODEL_FILE}",
    "config.json": "config.json",
    "tokenizer.json": "tokenizer.json",
    "tokenizer_config.json": "tokenizer_config.json",
    "special_tokens_map.json": "special_tokens_map.json",
}

MAX_QUERY_CHARS = 1_000
MAX_DOCUMENT_CHARS = 120_000
# The pinned tokenizer advertises a 512-token model context.  This bound
# includes the model's special tokens and the E5 query/passage prefix.
MAX_CHUNK_TOKENS = 512
EMBED_BATCH_SIZE = 8
INDEX_BATCH_SIZE = 16
INDEX_LEASE_SECONDS = 300
MAX_SEARCH_LIMIT = 100
RRF_K = 60
MAX_ARTIFACT_BYTES = 512 * 1024 * 1024

_BACKEND_CACHE: dict[tuple[str, int], "FastEmbedBackend"] = {}
_BACKEND_CACHE_LOCK = threading.Lock()
_CUSTOM_MODEL_REGISTRATION_LOCK = threading.Lock()
_PREPARE_LOCK = threading.Lock()
_ARTIFACT_STATUS_CACHE: dict[
    str, tuple[tuple[tuple[str, int, int, int, int], ...] | None, dict[str, str]]
] = {}
_ARTIFACT_STATUS_LOCK = threading.Lock()


class SemanticError(RuntimeError):
    """A concise semantic-layer failure suitable for CLI and MCP metadata."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@runtime_checkable
class EmbeddingBackend(Protocol):
    """Strict injectable backend contract used by semantic tests and callers.

    ``embed`` receives non-empty, already redacted strings and returns one raw,
    finite 768-dimensional vector per input.  It must not normalize those
    vectors.  This module averages document chunks, then applies L2
    normalization once to each document and query vector.
    """

    @property
    def ready(self) -> bool:
        """Whether this backend can encode without downloading a model."""

    @property
    def unavailable_code(self) -> str | None:
        """A stable reason when ``ready`` is false."""

    def split(self, text: str, *, prefix: str = "") -> Sequence[str]:
        """Return complete prefixed chunks within the 512-token model limit."""

    def embed(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
        """Encode a bounded batch of local text without network access."""


class FastEmbedBackend:
    """CPU-only FastEmbed 0.8.0 backend bound to verified local artifacts."""

    def __init__(self, model_dir: str | Path | None = None, *, threads: int | None = None) -> None:
        self.model_dir = _model_dir(model_dir)
        self.threads = _validated_threads(threads)
        self._engine: Any | None = None
        self._planner_tokenizer: Any | None = None
        self._engine_lock = threading.Lock()
        self._tokenizer_lock = threading.Lock()
        self._encode_lock = threading.Lock()

    @property
    def ready(self) -> bool:
        return semantic_status(self.model_dir)["status"] == "ready"

    @property
    def unavailable_code(self) -> str | None:
        status = semantic_status(self.model_dir)
        code = status.get("code")
        return code if isinstance(code, str) else None

    def split(self, text: str, *, prefix: str = "") -> list[str]:
        """Use the verified tokenizer offsets to retain every input character."""

        if not isinstance(text, str) or not text or len(text) > MAX_DOCUMENT_CHARS:
            raise SemanticError("invalid_embedding_input")
        return _split_with_tokenizer(self._get_planner_tokenizer(), text, prefix=prefix)

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        checked = _checked_text_batch(texts)
        engine = self._get_engine()
        try:
            # FastEmbed 0.8.0 accepts this exact API.  ``parallel=None`` keeps
            # work inside the fixed ONNX thread budget instead of spawning a
            # process pool for a small local queue batch.
            with self._encode_lock:
                raw_vectors = list(
                    engine.embed(checked, batch_size=EMBED_BATCH_SIZE, parallel=None)
                )
        except SemanticError:
            raise
        except Exception as exc:
            raise SemanticError("embedding_failed") from exc
        return [_checked_vector(vector) for vector in raw_vectors]

    def _get_engine(self) -> Any:
        state = _artifact_status(self.model_dir)
        if state["status"] != "ready":
            raise SemanticError(str(state["code"]))

        with self._engine_lock:
            if self._engine is not None:
                return self._engine
            if importlib.util.find_spec("fastembed") is None:
                raise SemanticError("dependency_missing")
            try:
                actual_fastembed_version = importlib.metadata.version("fastembed")
                actual_onnxruntime_version = importlib.metadata.version("onnxruntime")
            except importlib.metadata.PackageNotFoundError as exc:
                raise SemanticError("dependency_missing") from exc
            if actual_fastembed_version != FASTEMBED_VERSION:
                raise SemanticError("dependency_version_mismatch")
            if actual_onnxruntime_version != ONNXRUNTIME_VERSION:
                raise SemanticError("dependency_version_mismatch")

            try:
                with _offline_huggingface():
                    from fastembed import TextEmbedding
                    from fastembed.common.model_description import ModelSource, PoolingType

                    _register_e5_base(TextEmbedding, ModelSource, PoolingType)

                    self._engine = TextEmbedding(
                        model_name=MODEL,
                        specific_model_path=str(self.model_dir),
                        local_files_only=True,
                        providers=["CPUExecutionProvider"],
                        threads=self.threads,
                        cuda=False,
                    )
            except SemanticError:
                raise
            except Exception as exc:
                raise SemanticError("runtime_unavailable") from exc
            return self._engine

    def _get_planner_tokenizer(self) -> Any:
        state = _artifact_status(self.model_dir)
        if state["status"] != "ready":
            raise SemanticError(str(state["code"]))
        dependency_code = _dependency_status()
        if dependency_code is not None:
            raise SemanticError(dependency_code)
        with self._tokenizer_lock:
            if self._planner_tokenizer is not None:
                return self._planner_tokenizer
            try:
                # The FastEmbed live tokenizer has truncation enabled from
                # tokenizer_config.json.  Chunk planning needs the exact same
                # vocabulary and offsets without truncating a long document,
                # so it uses an isolated tokenizer instance.
                from tokenizers import Tokenizer

                tokenizer = Tokenizer.from_file(str(self.model_dir / "tokenizer.json"))
                tokenizer.no_truncation()
                tokenizer.no_padding()
            except Exception as exc:
                raise SemanticError("runtime_unavailable") from exc
            self._planner_tokenizer = tokenizer
            return tokenizer


def default_model_dir() -> Path:
    """Return the shared immutable-location model cache without creating it."""

    return Path.home() / ".cache" / "codex-mem" / "models" / MODEL_REVISION


def semantic_status(model_dir: str | Path | None = None) -> dict[str, Any]:
    """Report whether the exact local model and runtime are ready, offline only."""

    directory = _model_dir(model_dir)
    state = _artifact_status(directory)
    result: dict[str, Any] = {
        "status": state["status"],
        "model": MODEL,
        "revision": MODEL_REVISION,
        "dimensions": DIMENSIONS,
        "model_dir": str(directory),
    }
    if state["status"] != "ready":
        result["code"] = state["code"]
        return result

    dependency_code = _dependency_status()
    if dependency_code is not None:
        result["status"] = "unavailable"
        result["code"] = dependency_code
        return result
    return result


def prepare_model(model_dir: str | Path | None = None) -> dict[str, Any]:
    """Explicitly download and verify the five pinned model artifacts.

    This is the sole networked function in the semantic module.  Search,
    indexing, hooks, and status inspection only use the already verified local
    directory.
    """

    try:
        directory = _model_dir(model_dir)
    except (OSError, RuntimeError, TypeError, ValueError):
        return _prepare_receipt("failed", "invalid_model_dir")

    try:
        with _PREPARE_LOCK:
            directory.mkdir(parents=True, exist_ok=True)
            try:
                directory.chmod(0o700)
            except OSError:
                pass
            for filename in MODEL_FILES:
                destination = directory / filename
                expected = _CHECKSUMS.get(filename)
                if _artifact_valid(destination, expected):
                    continue
                _download_artifact(destination, filename, expected)
            artifact_state = _artifact_status(directory)
    except SemanticError as exc:
        return _prepare_receipt("failed", exc.code)
    except (HTTPError, URLError, OSError, ValueError):
        return _prepare_receipt("failed", "download_failed")
    except Exception:
        return _prepare_receipt("failed", "download_failed")

    if artifact_state["status"] != "ready":
        return _prepare_receipt("failed", str(artifact_state["code"]))
    return semantic_status(directory)


def index_pending(
    project: str | Path,
    data_dir: str | Path | None = None,
    *,
    backend: EmbeddingBackend | None = None,
    retry_failed: bool = False,
) -> dict[str, Any]:
    """Claim, encode, and finish at most one local embedding batch.

    A backend is checked before any row is leased.  Therefore a missing model
    never turns pending work into a failed embedding job.
    """

    workspace = project_key(project)
    if not isinstance(retry_failed, bool):
        raise ValueError("retry_failed must be a boolean")
    active_backend = _backend(backend)
    if not active_backend.ready:
        return _index_receipt("unavailable", 0, 0, active_backend.unavailable_code or "model_not_ready")

    try:
        with Store(data_dir) as store:
            claimed = store.claim_embedding_batch(
                workspace,
                MODEL,
                MODEL_REVISION,
                DIMENSIONS,
                limit=INDEX_BATCH_SIZE,
                max_chars=MAX_DOCUMENT_CHARS,
                lease_seconds=INDEX_LEASE_SECONDS,
                retry_failed=retry_failed,
            )
            if claimed is None:
                return _index_receipt("idle", 0, _pending_count(store, workspace))
            job_id, lease_token, entries = _claimed_parts(claimed)
            try:
                vectors = _vectors_for_entries(entries, active_backend)
                completed = store.complete_embedding_batch(
                    workspace,
                    job_id,
                    lease_token,
                    vectors=vectors,
                )
                indexed = _completed_count(completed)
                return _index_receipt("indexed", indexed, _pending_count(store, workspace))
            except SemanticError as exc:
                _fail_claim(store, workspace, job_id, lease_token, exc.code)
                return _index_receipt("failed", 0, _pending_count(store, workspace), exc.code)
            except (OSError, TypeError, ValueError):
                _fail_claim(store, workspace, job_id, lease_token, "indexing_failed")
                return _index_receipt("failed", 0, _pending_count(store, workspace), "indexing_failed")
    except (OSError, TypeError, ValueError):
        return _index_receipt("failed", 0, 0, "storage_failure")


def search(
    store: Store,
    project: str | Path,
    query: str,
    *,
    mode: str = "hybrid",
    intent: str = "lookup",
    limit: int = 10,
    kinds: Sequence[str] | None = None,
    types: Sequence[str] | None = None,
    concepts: Sequence[str] | None = None,
    files: Sequence[str] | None = None,
    backend: EmbeddingBackend | None = None,
) -> dict[str, Any]:
    """Search lexical, semantic, or hybrid previews with stable RRF ordering."""

    workspace = project_key(project)
    requested_mode = _checked_mode(mode)
    checked_limit = _checked_limit(limit)
    checked_intent = _checked_intent(intent)
    retrieval_limit = min(MAX_SEARCH_LIMIT, checked_limit * 4) if checked_intent == "resume" else checked_limit
    checked_query = _checked_query(query)
    checked_kinds = _checked_filter_values(kinds, "kinds") if kinds is not None else None
    checked_types = _checked_filter_values(types, "types") if types is not None else None
    checked_concepts = _checked_filter_values(concepts, "concepts") if concepts is not None else None
    checked_files = _checked_filter_values(files, "files") if files is not None else None

    def receipt(
        results: list[dict[str, Any]], used_mode: str, fallback_reason: str | None
    ) -> dict[str, Any]:
        if checked_intent == "resume":
            results = sorted(results[:retrieval_limit], key=resume_priority)[:checked_limit]
        return _search_receipt(results, requested_mode, used_mode, fallback_reason, checked_intent)

    if requested_mode == "lexical":
        results = _lexical_results(
            store,
            workspace,
            checked_query,
            retrieval_limit,
            checked_kinds,
            checked_types,
            checked_concepts,
            checked_files,
        )
        return receipt(results, "lexical", None)

    active_backend = _backend(backend)
    if not active_backend.ready:
        code = active_backend.unavailable_code or "model_not_ready"
        if requested_mode == "auto":
            results = _lexical_results(
                store,
                workspace,
                checked_query,
                retrieval_limit,
                checked_kinds,
                checked_types,
                checked_concepts,
                checked_files,
            )
            return receipt(results, "lexical", code)
        raise SemanticError(code)

    try:
        query_vector = _normalized_query_vector(active_backend, checked_query)
        if requested_mode == "semantic":
            results = _semantic_results(
                store,
                workspace,
                query_vector,
                retrieval_limit,
                checked_kinds,
                checked_types,
                checked_concepts,
                checked_files,
            )
            return receipt(results, "semantic", None)

        candidate_limit = min(MAX_SEARCH_LIMIT, max(checked_limit, checked_limit * 4))
        lexical = _lexical_results(
            store,
            workspace,
            checked_query,
            candidate_limit,
            checked_kinds,
            checked_types,
            checked_concepts,
            checked_files,
        )
        semantic = _semantic_results(
            store,
            workspace,
            query_vector,
            candidate_limit,
            checked_kinds,
            checked_types,
            checked_concepts,
            checked_files,
        )
        results = _rrf(lexical, semantic, retrieval_limit)
        used_mode = "hybrid"
        return receipt(results, used_mode, None)
    except SemanticError as exc:
        if requested_mode != "auto":
            raise
        results = _lexical_results(
            store,
            workspace,
            checked_query,
            retrieval_limit,
            checked_kinds,
            checked_types,
            checked_concepts,
            checked_files,
        )
        return receipt(results, "lexical", exc.code)


def _model_dir(model_dir: str | Path | None) -> Path:
    candidate = default_model_dir() if model_dir is None else Path(model_dir).expanduser()
    if not str(candidate).strip() or "\x00" in str(candidate):
        raise ValueError("model_dir must be a usable path")
    return candidate.resolve(strict=False)


def _validated_threads(threads: int | None) -> int:
    if threads is None:
        return min(2, max(1, os.cpu_count() or 1))
    if isinstance(threads, bool) or not isinstance(threads, int) or not 1 <= threads <= 2:
        raise ValueError("threads must be 1 or 2")
    return threads


def _artifact_status(directory: Path) -> dict[str, str]:
    fingerprint = _artifact_fingerprint(directory)
    key = str(directory)
    with _ARTIFACT_STATUS_LOCK:
        cached = _ARTIFACT_STATUS_CACHE.get(key)
        if cached is not None and cached[0] == fingerprint:
            return dict(cached[1])

    result = _artifact_status_uncached(directory, fingerprint)
    with _ARTIFACT_STATUS_LOCK:
        _ARTIFACT_STATUS_CACHE[key] = (fingerprint, dict(result))
    return result


def _artifact_fingerprint(
    directory: Path,
) -> tuple[tuple[str, int, int, int, int], ...] | None:
    """Return the stat fields that make a cached SHA-256 verification valid."""

    if not directory.is_dir() or directory.is_symlink():
        return None
    try:
        children = sorted(directory.iterdir(), key=lambda child: child.name)
        fingerprint: list[tuple[str, int, int, int, int]] = []
        for child in children:
            stat = child.lstat()
            fingerprint.append(
                (child.name, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
            )
    except OSError:
        return None
    return tuple(fingerprint)


def _artifact_status_uncached(
    directory: Path, fingerprint: tuple[tuple[str, int, int, int, int], ...] | None
) -> dict[str, str]:
    if fingerprint is None:
        return {"status": "unavailable", "code": "model_not_ready"}
    children = {item[0] for item in fingerprint}
    if children.difference(MODEL_FILES):
        return {"status": "unavailable", "code": "model_layout_invalid"}
    for filename in MODEL_FILES:
        path = directory / filename
        if not path.is_file() or path.is_symlink():
            return {"status": "unavailable", "code": "model_not_ready"}
        expected = _CHECKSUMS.get(filename)
        if expected is not None and not _artifact_valid(path, expected):
            return {"status": "unavailable", "code": "model_hash_mismatch"}
    return {"status": "ready"}


def _artifact_valid(path: Path, expected_sha256: str | None) -> bool:
    if not path.is_file() or path.is_symlink():
        return False
    try:
        if path.stat().st_size <= 0:
            return False
        if expected_sha256 is None:
            return True
        return _sha256(path) == expected_sha256
    except OSError:
        return False


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _dependency_status() -> str | None:
    if importlib.util.find_spec("fastembed") is None:
        return "dependency_missing"
    try:
        fastembed_version = importlib.metadata.version("fastembed")
        onnxruntime_version = importlib.metadata.version("onnxruntime")
    except importlib.metadata.PackageNotFoundError:
        return "dependency_missing"
    if fastembed_version != FASTEMBED_VERSION or onnxruntime_version != ONNXRUNTIME_VERSION:
        return "dependency_version_mismatch"
    return None


def _register_e5_base(text_embedding: Any, model_source: Any, pooling_type: Any) -> None:
    """Register the pinned E5-base ONNX layout once per process.

    FastEmbed 0.8.0 does not ship this model in its built-in registry.  Its
    supported custom-model API supplies the correct mean pooling while this
    module retains normalization until after multi-chunk aggregation.
    """

    with _CUSTOM_MODEL_REGISTRATION_LOCK:
        supported = text_embedding.list_supported_models()
        if any(
            isinstance(description, Mapping)
            and description.get("model", "").casefold() == MODEL.casefold()
            for description in supported
        ):
            return
        text_embedding.add_custom_model(
            model=MODEL,
            pooling=pooling_type.MEAN,
            normalization=False,
            sources=model_source(hf=MODEL_REPOSITORY),
            dim=DIMENSIONS,
            model_file=MODEL_FILE,
            description="Pinned local multilingual E5-base retrieval model",
            license="mit",
            size_in_gb=0.28,
        )


def _download_artifact(destination: Path, filename: str, expected_sha256: str | None) -> None:
    if filename not in MODEL_FILES or destination.name != filename:
        raise SemanticError("invalid_artifact")
    destination.parent.mkdir(parents=True, exist_ok=True)
    request = Request(
        "https://huggingface.co/"
        f"{MODEL_REPOSITORY}/resolve/{MODEL_REVISION}/{_REMOTE_ARTIFACTS[filename]}",
        headers={"User-Agent": "codex-mem-local/semantic-1"},
    )
    temporary_name: str | None = None
    try:
        with urlopen(request, timeout=60) as response:
            content_length = response.headers.get("Content-Length")
            if content_length is not None:
                try:
                    if int(content_length) > MAX_ARTIFACT_BYTES:
                        raise SemanticError("artifact_too_large")
                except ValueError as exc:
                    raise SemanticError("download_failed") from exc
            with tempfile.NamedTemporaryFile(
                dir=destination.parent, prefix=f".{filename}.", suffix=".part", delete=False
            ) as handle:
                temporary_name = handle.name
                written = 0
                while True:
                    block = response.read(1024 * 1024)
                    if not block:
                        break
                    written += len(block)
                    if written > MAX_ARTIFACT_BYTES:
                        raise SemanticError("artifact_too_large")
                    handle.write(block)
        temporary = Path(temporary_name)
        if not _artifact_valid(temporary, expected_sha256):
            raise SemanticError("model_hash_mismatch" if expected_sha256 else "download_failed")
        try:
            temporary.chmod(0o600)
        except OSError:
            pass
        os.replace(temporary, destination)
        temporary_name = None
    finally:
        if temporary_name is not None:
            try:
                Path(temporary_name).unlink(missing_ok=True)
            except OSError:
                pass


@contextmanager
def _offline_huggingface() -> Any:
    """Set Hugging Face's offline guard only while constructing FastEmbed."""

    previous = os.environ.get("HF_HUB_OFFLINE")
    os.environ["HF_HUB_OFFLINE"] = "1"
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("HF_HUB_OFFLINE", None)
        else:
            os.environ["HF_HUB_OFFLINE"] = previous


def _backend(backend: EmbeddingBackend | None) -> EmbeddingBackend:
    if backend is not None:
        if not isinstance(backend, EmbeddingBackend):
            raise TypeError("backend must implement EmbeddingBackend")
        return backend
    directory = _model_dir(None)
    threads = _validated_threads(None)
    key = (str(directory), threads)
    with _BACKEND_CACHE_LOCK:
        cached = _BACKEND_CACHE.get(key)
        if cached is None:
            cached = FastEmbedBackend(directory, threads=threads)
            _BACKEND_CACHE[key] = cached
        return cached


def _checked_text_batch(texts: Sequence[str]) -> list[str]:
    if isinstance(texts, (str, bytes, bytearray)) or not isinstance(texts, Sequence):
        raise SemanticError("invalid_embedding_input")
    if not texts or len(texts) > EMBED_BATCH_SIZE:
        raise SemanticError("invalid_embedding_input")
    checked: list[str] = []
    for text in texts:
        if (
            not isinstance(text, str)
            or not text
            or len(text) > MAX_DOCUMENT_CHARS + len(PASSAGE_PREFIX)
        ):
            raise SemanticError("invalid_embedding_input")
        checked.append(text)
    return checked


def _checked_vector(vector: Sequence[float]) -> list[float]:
    if isinstance(vector, (str, bytes, bytearray)):
        raise SemanticError("invalid_embedding")
    try:
        values = [float(item) for item in vector]
    except (TypeError, ValueError) as exc:
        raise SemanticError("invalid_embedding") from exc
    if len(values) != DIMENSIONS or any(not math.isfinite(item) for item in values):
        raise SemanticError("invalid_embedding")
    return values


def _checked_query(query: str) -> str:
    if not isinstance(query, str) or "\x00" in query:
        raise ValueError("query must be text")
    redacted = redact_text(query.strip())
    if not redacted:
        raise ValueError("query must not be empty")
    if len(redacted) > MAX_QUERY_CHARS:
        raise ValueError("query is too long")
    return redacted


def _checked_intent(intent: str) -> str:
    if not isinstance(intent, str) or intent not in {"lookup", "resume"}:
        raise ValueError("intent must be lookup or resume")
    return intent


def resume_priority(record: Mapping[str, Any]) -> int:
    """Prefer useful handoff records without asserting freshness or truth."""
    kind = record.get("kind")
    if kind == "session_summary" or record.get("session_summary"):
        return 0
    observation = record.get("observation")
    observation_type = observation.get("type") if isinstance(observation, Mapping) else None
    if kind in {"decision", "bugfix", "security_alert"} or observation_type in {"decision", "bugfix", "security_alert"}:
        return 1
    if kind in {"session", "tool", "checkpoint"}:
        return 3
    return 2


def _checked_mode(mode: str) -> str:
    if mode not in {"auto", "lexical", "semantic", "hybrid"}:
        raise ValueError("mode must be auto, lexical, semantic, or hybrid")
    return mode


def _checked_limit(limit: int) -> int:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_SEARCH_LIMIT:
        raise ValueError(f"limit must be between 1 and {MAX_SEARCH_LIMIT}")
    return limit


def _checked_filter_values(
    value: Sequence[str] | str | None,
    field: str,
    *,
    maximum_items: int = 100,
    maximum_chars: int = 1_000,
) -> list[str] | None:
    """Validate a structured retrieval filter before it reaches storage.

    Storage owns the canonical validation and redaction of metadata.  The
    semantic layer still validates the transport shape so an embedding search
    cannot accidentally interpret a scalar, mapping, or bytes object as an
    iterable of filter values.  Returning a fresh list also prevents a caller
    from mutating filters while lexical and semantic branches are running.
    """

    if value is None:
        return None
    if isinstance(value, str):
        values: list[object] = [value]
    elif isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        values = list(value)
    else:
        raise ValueError(f"{field} must be a list of text values")
    if not values:
        raise ValueError(f"{field} must not be empty")
    if len(values) > maximum_items:
        raise ValueError(f"{field} has too many values")
    checked: list[str] = []
    seen: set[str] = set()
    for candidate in values:
        if not isinstance(candidate, str) or "\x00" in candidate:
            raise ValueError(f"{field} must contain text values")
        cleaned = candidate.strip()
        if not cleaned:
            raise ValueError(f"{field} must not contain empty values")
        if len(cleaned) > maximum_chars:
            raise ValueError(f"{field} contains a value that is too long")
        if cleaned not in seen:
            checked.append(cleaned)
            seen.add(cleaned)
    if not checked:
        raise ValueError(f"{field} must not be empty")
    return checked


def _filter_kwargs(
    kinds: Sequence[str] | str | None,
    types: Sequence[str] | str | None,
    concepts: Sequence[str] | str | None,
    files: Sequence[str] | str | None,
) -> dict[str, list[str] | None]:
    """Build only the filter kwargs requested by the caller.

    Omitting new kwargs when they are absent keeps the v1/v2 in-process Store
    seam and third-party test doubles source compatible.  Once a structured
    filter is requested it is passed to storage as a candidate-universe
    constraint; callers must not emulate this by filtering an already limited
    result list.
    """

    values: dict[str, list[str] | None] = {}
    if kinds is not None:
        values["kinds"] = _checked_filter_values(kinds, "kinds")
    if types is not None:
        values["types"] = _checked_filter_values(types, "types")
    if concepts is not None:
        values["concepts"] = _checked_filter_values(concepts, "concepts")
    if files is not None:
        values["files"] = _checked_filter_values(files, "files")
    return values


def _claimed_parts(claimed: object) -> tuple[str, str, list[Mapping[str, Any]]]:
    if not isinstance(claimed, Mapping):
        raise SemanticError("storage_protocol_error")
    job_id = claimed.get("job_id")
    lease_token = claimed.get("lease_token")
    entries = claimed.get("entries")
    if not isinstance(job_id, str) or not job_id or not isinstance(lease_token, str) or not lease_token:
        raise SemanticError("storage_protocol_error")
    if not isinstance(entries, Sequence) or isinstance(entries, (str, bytes, bytearray)) or not entries:
        raise SemanticError("storage_protocol_error")
    if len(entries) > INDEX_BATCH_SIZE or any(not isinstance(entry, Mapping) for entry in entries):
        raise SemanticError("storage_protocol_error")
    return job_id, lease_token, list(entries)


def _vectors_for_entries(
    entries: Sequence[Mapping[str, Any]], backend: EmbeddingBackend
) -> list[dict[str, Any]]:
    vectors: list[dict[str, Any]] = []
    for entry in entries:
        entry_id = entry.get("id")
        content_hash = entry.get("content_hash")
        if not isinstance(entry_id, str) or not entry_id or not isinstance(content_hash, str) or not content_hash:
            raise SemanticError("storage_protocol_error")
        document = _entry_document(entry)
        chunks = _backend_chunks(backend, document, prefix=PASSAGE_PREFIX)
        embedded = _embed_chunks(backend, chunks)
        vectors.append(
            {
                "entry_id": entry_id,
                "content_hash": content_hash,
                "vector": _normalize(_mean(embedded)),
            }
        )
    return vectors


def _entry_document(entry: Mapping[str, Any]) -> str:
    encoded_text = entry.get("text")
    if encoded_text is not None:
        if not isinstance(encoded_text, str) or not encoded_text:
            raise SemanticError("storage_protocol_error")
        if redact_text(encoded_text) != encoded_text or len(encoded_text) > MAX_DOCUMENT_CHARS:
            raise SemanticError("storage_protocol_error")
        content_hash = entry.get("content_hash")
        expected_hash = hashlib.sha256(encoded_text.encode("utf-8")).hexdigest()
        if content_hash != expected_hash:
            raise SemanticError("storage_protocol_error")
        return encoded_text

    # Store v3 sends ``text`` above.  The structured fallback keeps the
    # injected-backend seam usable while an older in-process Store finishes a
    # migration, without changing what a v3 content hash describes.
    title = entry.get("title")
    body = entry.get("body")
    tags = entry.get("tags")
    if not isinstance(title, str) or not isinstance(body, str):
        raise SemanticError("storage_protocol_error")
    if not isinstance(tags, Sequence) or isinstance(tags, (str, bytes, bytearray)):
        raise SemanticError("storage_protocol_error")
    if any(not isinstance(tag, str) for tag in tags):
        raise SemanticError("storage_protocol_error")
    parts = [title]
    if tags:
        parts.append("Tags: " + ", ".join(tags))
    parts.append(body)
    document = redact_text("\n\n".join(parts).strip())
    if not document or len(document) > MAX_DOCUMENT_CHARS:
        raise SemanticError("document_too_large")
    return document


def _normalized_query_vector(backend: EmbeddingBackend, query: str) -> list[float]:
    chunks = _backend_chunks(backend, query, prefix=QUERY_PREFIX)
    return _normalize(_mean(_embed_chunks(backend, chunks)))


def _backend_chunks(backend: EmbeddingBackend, text: str, *, prefix: str = "") -> list[str]:
    try:
        raw_chunks = backend.split(text, prefix=prefix)
    except SemanticError:
        raise
    except Exception as exc:
        raise SemanticError("chunking_failed") from exc
    if isinstance(raw_chunks, (str, bytes, bytearray)) or not isinstance(raw_chunks, Sequence):
        raise SemanticError("invalid_chunking")
    chunks = list(raw_chunks)
    if not chunks or any(not isinstance(chunk, str) or not chunk for chunk in chunks):
        raise SemanticError("invalid_chunking")
    if any(len(chunk) > MAX_DOCUMENT_CHARS + len(PASSAGE_PREFIX) for chunk in chunks):
        raise SemanticError("invalid_chunking")
    if prefix and any(not chunk.startswith(prefix) for chunk in chunks):
        raise SemanticError("invalid_chunking")
    return chunks


def _embed_chunks(backend: EmbeddingBackend, chunks: Sequence[str]) -> list[list[float]]:
    embedded: list[list[float]] = []
    for position in range(0, len(chunks), EMBED_BATCH_SIZE):
        batch = chunks[position : position + EMBED_BATCH_SIZE]
        raw_vectors = backend.embed(batch)
        if len(raw_vectors) != len(batch):
            raise SemanticError("invalid_embedding")
        embedded.extend(_checked_vector(vector) for vector in raw_vectors)
    return embedded


def _split_with_tokenizer(tokenizer: Any, text: str, *, prefix: str = "") -> list[str]:
    """Use untruncated token offsets to partition every original character.

    ``Tokenizer`` offsets refer to the unprefixed input string.  Every output
    chunk carries the supplied E5 prefix, and the boundary reserves room for
    that prefix plus the model's special tokens.  A candidate is tokenized
    again with its prefix to handle a wordpiece that changes at a string edge.
    The cursor always advances over original characters, so the final tail is
    encoded instead of being silently truncated.
    """

    if not isinstance(text, str) or not text:
        raise SemanticError("invalid_document")
    if prefix not in {"", QUERY_PREFIX, PASSAGE_PREFIX}:
        raise SemanticError("invalid_embedding_prefix")
    prefix_encoding = _tokenizer_encode(tokenizer, prefix, add_special_tokens=False)
    prefix_ids = getattr(prefix_encoding, "ids", None)
    if not isinstance(prefix_ids, Sequence):
        raise SemanticError("tokenizer_failure")
    content_budget = MAX_CHUNK_TOKENS - 2 - len(prefix_ids)
    if content_budget < 1:
        raise SemanticError("tokenizer_failure")
    chunks: list[str] = []
    remaining = text
    while remaining:
        encoding = _tokenizer_encode(tokenizer, remaining, add_special_tokens=False)
        ids = getattr(encoding, "ids", None)
        offsets = getattr(encoding, "offsets", None)
        if not isinstance(ids, Sequence) or not isinstance(offsets, Sequence) or len(ids) != len(offsets):
            raise SemanticError("tokenizer_failure")
        if (
            len(ids) <= content_budget
            and _token_count(tokenizer, prefix + remaining, add_special_tokens=True)
            <= MAX_CHUNK_TOKENS
        ):
            chunks.append(prefix + remaining)
            break
        boundary = _token_boundary(offsets, content_budget - 1, len(remaining))
        candidate_end = _safe_chunk_end(tokenizer, remaining, boundary, prefix=prefix)
        if candidate_end <= 0:
            raise SemanticError("tokenizer_failure")
        candidate = remaining[:candidate_end]
        if not candidate:
            raise SemanticError("tokenizer_failure")
        chunks.append(prefix + candidate)
        remaining = remaining[candidate_end:]
    return chunks


def _tokenizer_encode(tokenizer: Any, text: str, *, add_special_tokens: bool) -> Any:
    try:
        return tokenizer.encode(text, add_special_tokens=add_special_tokens)
    except Exception as exc:
        raise SemanticError("tokenizer_failure") from exc


def _token_boundary(offsets: Sequence[object], index: int, text_length: int) -> int:
    """Return an advancing character boundary at or before one token budget."""

    for position in range(min(index, len(offsets) - 1), -1, -1):
        offset = offsets[position]
        if (
            isinstance(offset, Sequence)
            and not isinstance(offset, (str, bytes, bytearray))
            and len(offset) == 2
            and isinstance(offset[1], int)
            and 0 < offset[1] <= text_length
        ):
            return offset[1]
    raise SemanticError("tokenizer_failure")


def _token_count(tokenizer: Any, text: str, *, add_special_tokens: bool) -> int:
    ids = getattr(_tokenizer_encode(tokenizer, text, add_special_tokens=add_special_tokens), "ids", None)
    if not isinstance(ids, Sequence):
        raise SemanticError("tokenizer_failure")
    return len(ids)


def _safe_chunk_end(tokenizer: Any, text: str, boundary: int, *, prefix: str) -> int:
    """Find a boundary whose full prefixed model input fits the context."""

    end = boundary
    while end > 0:
        encoded = _tokenizer_encode(tokenizer, prefix + text[:end], add_special_tokens=True)
        ids = getattr(encoded, "ids", None)
        if isinstance(ids, Sequence) and len(ids) <= MAX_CHUNK_TOKENS:
            return end
        # A model token can cover several source characters.  Back up to the
        # preceding Unicode code point and retry rather than dropping it.
        end -= 1
    return 0


def _mean(vectors: Sequence[Sequence[float]]) -> list[float]:
    if not vectors:
        raise SemanticError("invalid_embedding")
    total = [0.0] * DIMENSIONS
    for vector in vectors:
        checked = _checked_vector(vector)
        for index, value in enumerate(checked):
            total[index] += value
    divisor = float(len(vectors))
    return [value / divisor for value in total]


def _normalize(vector: Sequence[float]) -> list[float]:
    checked = _checked_vector(vector)
    magnitude = math.sqrt(sum(value * value for value in checked))
    if not math.isfinite(magnitude) or magnitude == 0.0:
        raise SemanticError("invalid_embedding")
    return [value / magnitude for value in checked]


def _lexical_results(
    store: Store,
    project: str,
    query: str,
    limit: int,
    kinds: Sequence[str] | None,
    types: Sequence[str] | None = None,
    concepts: Sequence[str] | None = None,
    files: Sequence[str] | None = None,
) -> list[dict[str, Any]]:
    kwargs = {"limit": limit, **_filter_kwargs(kinds, types, concepts, files)}
    return _preview_list(store.search(project, query, **kwargs))


def _semantic_results(
    store: Store,
    project: str,
    query_vector: Sequence[float],
    limit: int,
    kinds: Sequence[str] | None,
    types: Sequence[str] | None = None,
    concepts: Sequence[str] | None = None,
    files: Sequence[str] | None = None,
) -> list[dict[str, Any]]:
    kwargs = {"limit": limit, **_filter_kwargs(kinds, types, concepts, files)}
    return _preview_list(
        store.semantic_search(
            project,
            list(query_vector),
            MODEL,
            MODEL_REVISION,
            DIMENSIONS,
            **kwargs,
        )
    )


def _preview_list(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise SemanticError("storage_protocol_error")
    previews: list[dict[str, Any]] = []
    seen: set[str] = set()
    for preview in value:
        if not isinstance(preview, Mapping):
            raise SemanticError("storage_protocol_error")
        record = dict(preview)
        entry_id = record.get("id")
        if not isinstance(entry_id, str) or not entry_id or entry_id in seen:
            raise SemanticError("storage_protocol_error")
        seen.add(entry_id)
        previews.append(record)
    return previews


def _rrf(
    lexical: Sequence[Mapping[str, Any]], semantic: Sequence[Mapping[str, Any]], limit: int
) -> list[dict[str, Any]]:
    scores: dict[str, float] = {}
    records: dict[str, dict[str, Any]] = {}
    for results in (lexical, semantic):
        for rank, result in enumerate(results, start=1):
            entry_id = result.get("id")
            if not isinstance(entry_id, str) or not entry_id:
                raise SemanticError("storage_protocol_error")
            scores[entry_id] = scores.get(entry_id, 0.0) + (1.0 / (RRF_K + rank))
            records.setdefault(entry_id, dict(result))
    ordered = sorted(scores, key=lambda entry_id: (-scores[entry_id], entry_id))[:limit]
    output: list[dict[str, Any]] = []
    for entry_id in ordered:
        record = records[entry_id]
        record["score"] = scores[entry_id]
        output.append(record)
    return output


def _pending_count(store: Store, project: str) -> int:
    status = store.embedding_status(project, MODEL, MODEL_REVISION, DIMENSIONS)
    if not isinstance(status, Mapping):
        raise SemanticError("storage_protocol_error")
    pending = status.get("pending")
    if isinstance(pending, bool) or not isinstance(pending, int) or pending < 0:
        raise SemanticError("storage_protocol_error")
    return pending


def _completed_count(completed: object) -> int:
    if not isinstance(completed, Mapping):
        raise SemanticError("storage_protocol_error")
    indexed = completed.get("indexed_count")
    if isinstance(indexed, bool) or not isinstance(indexed, int) or indexed < 0:
        raise SemanticError("storage_protocol_error")
    return indexed


def _fail_claim(store: Store, project: str, job_id: str, lease_token: str, code: str) -> None:
    try:
        store.fail_embedding_batch(project, job_id, lease_token, code)
    except (OSError, TypeError, ValueError):
        pass


def _index_receipt(
    status: str, indexed: int, pending: int, code: str | None = None
) -> dict[str, Any]:
    receipt: dict[str, Any] = {"status": status, "indexed": indexed, "pending": pending}
    if code is not None:
        receipt["code"] = code
    return receipt


def _search_receipt(
    results: list[dict[str, Any]],
    requested_mode: str,
    used_mode: str,
    fallback_reason: str | None,
    intent: str,
) -> dict[str, Any]:
    return {
        "results": results,
        "intent": intent,
        "requested_mode": requested_mode,
        "used_mode": used_mode,
        "fallback_reason": fallback_reason,
        "model": MODEL,
        "revision": MODEL_REVISION,
    }


def _prepare_receipt(status: str, code: str) -> dict[str, Any]:
    return {
        "status": status,
        "code": code,
        "model": MODEL,
        "revision": MODEL_REVISION,
        "dimensions": DIMENSIONS,
    }
