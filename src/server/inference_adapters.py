"""Validate text generation and dense embeddings over the admitted provider transport."""

from __future__ import annotations

import json
import math
import re

from .provider_transport import ProviderError, ProviderTransport

_MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}")
_CHAT_ROLES = ("system", "user", "assistant")
_MAX_MESSAGES = 32
_MAX_EMBEDDING_INPUTS = 32
_MAX_INPUT_TEXT_BYTES = 8 * 1024
_MAX_REQUEST_BYTES = 32 * 1024
_MAX_CHAT_RESPONSE_BYTES = 256 * 1024
_MAX_EMBEDDING_RESPONSE_BYTES = 1024 * 1024
_MAX_OUTPUT_TEXT_BYTES = 16 * 1024
_MAX_OUTPUT_TOKENS = 256
_BGE_MODELS = ("BAAI/bge-m3", "Pro/BAAI/bge-m3")
_BGE_DIMENSIONS = 1024


def _valid_text(value: object, max_bytes: int) -> bool:
    if not isinstance(value, str) or len(value) > max_bytes or not value.strip():
        return False
    try:
        return len(value.encode("utf-8")) <= max_bytes
    except UnicodeEncodeError:
        return False


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError("Duplicate JSON field")
    return result


def _finite_float(value: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("Nonfinite JSON number")
    return result


def _post_json(
    transport: ProviderTransport, target: str, payload: dict, max_response_bytes: int,
    expected_object: str,
) -> dict:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(body) > _MAX_REQUEST_BYTES:
        raise ProviderError("PROVIDER_INVALID_INPUT")
    raw = transport.request("POST", target, body=body)
    if not isinstance(raw, bytes) or len(raw) > max_response_bytes:
        raise ProviderError("PROVIDER_INVALID_RESPONSE")
    try:
        value = json.loads(
            raw.decode("utf-8"), object_pairs_hook=_unique_object,
            parse_float=_finite_float, parse_constant=_finite_float,
        )
    except (ValueError, RecursionError):
        raise ProviderError("PROVIDER_INVALID_RESPONSE") from None
    if (not isinstance(value, dict) or "error" in value
            or value.get("object", expected_object) != expected_object):
        raise ProviderError("PROVIDER_INVALID_RESPONSE")
    if value.get("model") != payload["model"]:
        raise ProviderError("PROVIDER_MODEL_MISMATCH")
    return value


def _token_usage(value: object, *, chat: bool) -> dict[str, int] | None:
    if not isinstance(value, dict):
        return None
    fields = ["prompt_tokens", "total_tokens"]
    if chat or "completion_tokens" in value:
        fields.append("completion_tokens")
    usage = {field: value.get(field) for field in fields}
    if any(type(count) is not int or count < 0 for count in usage.values()):
        return None
    completion = usage.get("completion_tokens", 0)
    if (not chat and completion != 0) or usage["total_tokens"] != usage["prompt_tokens"] + completion:
        return None
    return usage


class ChatAdapter:
    """A pinned, text-only OpenAI-compatible chat completion."""

    def __init__(self, transport: ProviderTransport, model: str):
        if not isinstance(model, str) or not _MODEL_ID.fullmatch(model):
            raise ProviderError("PROVIDER_INVALID_INPUT")
        self._transport = transport
        self._model = model

    def complete(self, messages: list[dict]) -> dict:
        if not isinstance(messages, list) or not 1 <= len(messages) <= _MAX_MESSAGES:
            raise ProviderError("PROVIDER_INVALID_INPUT")
        validated_messages = []
        for message in messages:
            if not isinstance(message, dict) or message.keys() != {"role", "content"}:
                raise ProviderError("PROVIDER_INVALID_INPUT")
            role, content = message["role"], message["content"]
            if role not in _CHAT_ROLES or not _valid_text(content, _MAX_INPUT_TEXT_BYTES):
                raise ProviderError("PROVIDER_INVALID_INPUT")
            validated_messages.append({"role": role, "content": content})
        response = _post_json(
            self._transport, "/v1/chat/completions",
            {"model": self._model, "messages": validated_messages, "stream": False,
             "max_tokens": _MAX_OUTPUT_TOKENS},
            _MAX_CHAT_RESPONSE_BYTES, "chat.completion",
        )
        choices = response.get("choices")
        if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
            raise ProviderError("PROVIDER_INVALID_RESPONSE")
        choice = choices[0]
        index = choice.get("index", 0)
        if type(index) is not int or index != 0:
            raise ProviderError("PROVIDER_INVALID_RESPONSE")
        if choice.get("finish_reason") != "stop":
            raise ProviderError("PROVIDER_GENERATION_INCOMPLETE")
        message = choice.get("message")
        if not isinstance(message, dict) or message.get("role") != "assistant":
            raise ProviderError("PROVIDER_INVALID_RESPONSE")
        if (message.get("tool_calls") not in (None, []) or message.get("function_call") is not None
                or message.get("refusal") is not None or message.get("audio") is not None):
            raise ProviderError("PROVIDER_GENERATION_INCOMPLETE")
        content = message.get("content")
        if not _valid_text(content, _MAX_OUTPUT_TEXT_BYTES):
            raise ProviderError("PROVIDER_INVALID_RESPONSE")
        return {"model": self._model, "text": content, "usage": _token_usage(response.get("usage"), chat=True)}


def _embedding_vector(value: object) -> list[float]:
    if (not isinstance(value, list) or len(value) != _BGE_DIMENSIONS
            or any(type(item) not in (int, float) for item in value)):
        raise ProviderError("PROVIDER_INVALID_RESPONSE")
    try:
        vector = [float(item) for item in value]
    except OverflowError:
        raise ProviderError("PROVIDER_INVALID_RESPONSE") from None
    if not all(math.isfinite(item) for item in vector) or not any(vector):
        raise ProviderError("PROVIDER_INVALID_RESPONSE")
    return vector


class BgeM3Adapter:
    """SiliconFlow-compatible dense BGE-M3 embeddings in original input order."""

    def __init__(self, transport: ProviderTransport, model: str = "BAAI/bge-m3"):
        if model not in _BGE_MODELS:
            raise ProviderError("PROVIDER_INVALID_INPUT")
        self._transport = transport
        self._model = model

    def embed(self, texts: list[str]) -> dict:
        if not isinstance(texts, list) or not 1 <= len(texts) <= _MAX_EMBEDDING_INPUTS:
            raise ProviderError("PROVIDER_INVALID_INPUT")
        inputs = list(texts)
        if any(not _valid_text(text, _MAX_INPUT_TEXT_BYTES) for text in inputs):
            raise ProviderError("PROVIDER_INVALID_INPUT")
        response = _post_json(
            self._transport, "/v1/embeddings",
            {"model": self._model, "input": inputs, "encoding_format": "float"},
            _MAX_EMBEDDING_RESPONSE_BYTES, "list",
        )
        data = response.get("data")
        if not isinstance(data, list) or len(data) != len(inputs):
            raise ProviderError("PROVIDER_INVALID_RESPONSE")
        vectors: dict[int, list[float]] = {}
        for row in data:
            if not isinstance(row, dict) or row.get("object", "embedding") != "embedding":
                raise ProviderError("PROVIDER_INVALID_RESPONSE")
            index = row.get("index")
            if type(index) is not int or not 0 <= index < len(inputs) or index in vectors:
                raise ProviderError("PROVIDER_INVALID_RESPONSE")
            vectors[index] = _embedding_vector(row.get("embedding"))
        return {
            "model": self._model,
            "vectors": [vectors[index] for index in range(len(inputs))],
            "usage": _token_usage(response.get("usage"), chat=False),
        }
