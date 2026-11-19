"""CPU-only dense BGE-M3 from the previously verified offline snapshot."""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from hashlib import file_digest, sha256
from importlib.metadata import PackageNotFoundError, version
import json
import os
from pathlib import Path
from threading import Lock

from .inference_adapters import _embedding_vector, _valid_text
from .parsing.chunking import PROFILE as CHUNK_PROFILE
from .provider_transport import ProviderError


ADAPTER = "bge-m3-local-v1"
MODEL = "BAAI/bge-m3"
ENDPOINT_REF = "local-bge-m3"
MODEL_DIR_ENV = "DATACENTER_LOCAL_BGE_MODEL_DIR"
DIMENSIONS = 1024
_MAX_TOKENS = CHUNK_PROFILE["embedding_max_tokens"]
_MAX_TEXT_BYTES = CHUNK_PROFILE["embedding_max_bytes"]
_FILES = {
    "pytorch_model.bin": "b5e0ce3470abf5ef3831aa1bd5553b486803e83251590ab7ff35a117cf6aad38",
    "tokenizer.json": CHUNK_PROFILE["tokenizer_sha256"],
    "config.json": "26159e7ad065073448460117eb24b7a4572f6f4e78eadff65dc0a11c052449fa",
    "1_Pooling/config.json": "e54c164a07274f2eb45bb724f54a79d1efcc90c41573887cd9a29aeee0597352",
    "config_sentence_transformers.json": "1eef72430e7194a1e59680e635aed81ffa083f05668dbc5bb1c56c04c0999c38",
    "modules.json": "84e40c8e006c9b1d6c122e02cba9b02458120b5fb0c87b746c41e0207cf642cf",
    "sentence_bert_config.json": "eb9b44b13c0f52a3b3685c3b1cbdea1ba8b04bea123b98f61610048940776eb1",
    "sentencepiece.bpe.model": "cfc8146abe2a0488e9e2a0c56de7952f7c11ab059eca145a0a727afce0db2865",
    "special_tokens_map.json": "8c785abebea9ae3257b61681b4e6fd8365ceafde980c21970d001e834cf10835",
    "tokenizer_config.json": "a62b2b6784f990259fddef5f16388693a8043be4f69179e6a5257eeb3f9abac4",
}
_RUNTIME = {"sentence-transformers": "5.5.0", "torch": "2.12.0", "transformers": "5.8.1",
            "tokenizers": "0.22.2", "numpy": "2.4.2"}
_IGNORED = {".gitattributes", "README.md", "configuration.json", "colbert_linear.pt", "imgs", "long.jpg", "onnx", "sparse_linear.pt"}
_MODEL_LOCK = Lock()


def profile() -> dict:
    return {"adapter": ADAPTER, "model": MODEL, "dimensions": DIMENSIONS, "device": "cpu",
            "weights_sha256": _FILES["pytorch_model.bin"], "tokenizer_sha256": _FILES["tokenizer.json"],
            "artifact_manifest_sha256": sha256(json.dumps(_FILES, sort_keys=True).encode()).hexdigest(),
            "runtime": dict(_RUNTIME), "representation": "cls-l2-float32-v1", "max_tokens": _MAX_TOKENS}


def read_model_endpoint():
    raw = os.environ.get(MODEL_DIR_ENV)
    if not raw:
        return None
    try:
        directory = Path(raw)
        if not directory.is_absolute():
            raise OSError()
        directory = directory.resolve(strict=True)
        _model_files(directory)
    except (OSError, ValueError, RuntimeError):
        raise ProviderError("LOCAL_BGE_SNAPSHOT_UNAVAILABLE") from None
    return LocalBgeEndpoint(directory)


def _model_files(directory: Path) -> tuple:
    allowed = _IGNORED | {name.partition("/")[0] for name in _FILES}
    if any(item.name not in allowed for item in directory.iterdir()):
        raise ProviderError("LOCAL_BGE_SNAPSHOT_CHANGED")
    if {item.name for item in (directory / "1_Pooling").iterdir()} != {"config.json"}:
        raise ProviderError("LOCAL_BGE_SNAPSHOT_CHANGED")
    stats = []
    for name in _FILES:
        file = directory / name
        stat = file.stat()
        if not file.is_file():
            raise ProviderError("LOCAL_BGE_SNAPSHOT_CHANGED")
        stats.append((stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns))
    return tuple(stats)


@lru_cache(maxsize=1)
def _load(directory: Path, stats: tuple):
    try:
        if {package: version(package) for package in _RUNTIME} != _RUNTIME:
            raise ProviderError("LOCAL_BGE_RUNTIME_MISMATCH")
        for name, expected in _FILES.items():
            with (directory / name).open("rb") as stream:
                if file_digest(stream, "sha256").hexdigest() != expected:
                    raise ProviderError("LOCAL_BGE_SNAPSHOT_CHANGED")
        # Local absolute path, integrity-checked configuration and weights, and
        # explicit offline loading even when the host has no HF offline settings.
        import torch
        from sentence_transformers import SentenceTransformer

        if torch.version.cuda is not None or torch.version.hip is not None:
            raise ProviderError("LOCAL_BGE_RUNTIME_MISMATCH")
        model = SentenceTransformer(str(directory), device="cpu", local_files_only=True, trust_remote_code=False,
                                    model_kwargs={"use_safetensors": False}, processor_kwargs={"use_fast": True})
        if (model.get_embedding_dimension() != DIMENSIONS or not model.processor.is_fast
                or _model_files(directory) != stats):
            raise ProviderError("LOCAL_BGE_SNAPSHOT_CHANGED")
        model.max_seq_length = _MAX_TOKENS
        model.eval()
        return model
    except ProviderError:
        raise
    except (PackageNotFoundError, OSError, ValueError, ImportError, RuntimeError, AttributeError):
        raise ProviderError("LOCAL_BGE_LOAD_FAILED") from None


@dataclass(frozen=True)
class LocalBgeEndpoint:
    directory: Path

    def embed(self, texts: list[str]) -> dict:
        if (not isinstance(texts, list) or not 1 <= len(texts) <= 32
                or not all(_valid_text(text, _MAX_TEXT_BYTES) for text in texts)):
            raise ProviderError("PROVIDER_INVALID_INPUT")
        try:
            with _MODEL_LOCK:
                stats = _model_files(self.directory)
                model = _load(self.directory, stats)
                counts = model.processor(texts, add_special_tokens=True, padding=False,
                                         truncation=False, return_length=True)["length"]
                if any(count > _MAX_TOKENS for count in counts):
                    raise ProviderError("PROVIDER_INVALID_INPUT")
                vectors = model.encode(texts, batch_size=4, convert_to_numpy=True,
                                       normalize_embeddings=False, show_progress_bar=False).tolist()
                if _model_files(self.directory) != stats:
                    raise ProviderError("LOCAL_BGE_SNAPSHOT_CHANGED")
        except ProviderError:
            raise
        except (OSError, ValueError, RuntimeError, OverflowError):
            raise ProviderError("LOCAL_BGE_INFERENCE_FAILED") from None
        if len(vectors) != len(texts):
            raise ProviderError("PROVIDER_INVALID_RESPONSE")
        return {"model": MODEL, "vectors": [_embedding_vector(vector) for vector in vectors], "usage": None}
