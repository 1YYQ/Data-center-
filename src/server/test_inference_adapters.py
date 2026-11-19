from __future__ import annotations

import json

import pytest

from .inference_adapters import BgeM3Adapter, ChatAdapter
from .provider_transport import ProviderError

_MESSAGES = [{"role": "system", "content": "Synthetic fixture."}, {"role": "user", "content": "你好"}]
_CHAT_MODEL = "synthetic/chat-v1"


class FakeTransport:
    def __init__(self, response: bytes | dict | ProviderError):
        self.response = json.dumps(response).encode() if isinstance(response, dict) else response
        self.requests = []

    def request(self, method, target, *, body=b"", purpose="api", content_type="application/json"):
        self.requests.append((method, target, json.loads(body), purpose, content_type))
        if isinstance(self.response, ProviderError):
            raise self.response
        return self.response


def _chat_response(**overrides):
    return {
        "object": "chat.completion",
        "model": _CHAT_MODEL,
        "id": "upstream-only",
        "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "  好。\n"}}],
        "usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10, "extra": "upstream-only"},
        **overrides,
    }


def _vector(first=1):
    return [first] + [0] * 1023


def _embedding_response(**overrides):
    return {
        "object": "list",
        "model": "BAAI/bge-m3",
        "data": [{"object": "embedding", "index": 0, "embedding": _vector()}],
        "usage": {"prompt_tokens": 3, "total_tokens": 3},
        **overrides,
    }


def test_chat_sends_only_fixed_options_and_returns_validated_fields():
    transport = FakeTransport(_chat_response())
    assert ChatAdapter(transport, _CHAT_MODEL).complete(_MESSAGES) == {
        "text": "  好。\n", "model": _CHAT_MODEL,
        "usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
    }
    assert transport.requests == [("POST", "/v1/chat/completions", {
        "model": _CHAT_MODEL, "messages": _MESSAGES, "stream": False, "max_tokens": 256,
    }, "api", "application/json")]


@pytest.mark.parametrize("model", [None, "", "bad model", "x" * 129])
def test_chat_rejects_unusable_model_id_before_transport(model):
    transport = FakeTransport(b"")
    with pytest.raises(ProviderError, match=r"^PROVIDER_INVALID_INPUT$"):
        ChatAdapter(transport, model)
    assert transport.requests == []


@pytest.mark.parametrize("messages", [
    None, [], _MESSAGES * 17, [None], [{"role": "tool", "content": "x"}],
    [{"role": "user", "content": "x", "options": {"stream": True}}],
    [{"role": "user", "content": ["x"]}], [{"role": "user", "content": "\n"}],
    [{"role": "user", "content": "\ud800"}], [{"role": "user", "content": "字" * 3000}],
    [{"role": "user", "content": "x" * 7000}] * 5,
], ids=["not-list", "empty", "too-many", "not-message", "tool-role", "extra-fields",
        "structured-content", "whitespace", "invalid-utf8", "text-bytes", "request-bytes"])
def test_chat_invalid_input_never_reaches_transport(messages):
    transport = FakeTransport(b"")
    with pytest.raises(ProviderError, match=r"^PROVIDER_INVALID_INPUT$"):
        ChatAdapter(transport, _CHAT_MODEL).complete(messages)
    assert transport.requests == []


@pytest.mark.parametrize(("raw", "code"), [
    (b'{"secret-upstream-body":', "PROVIDER_INVALID_RESPONSE"),
    (b'"\xff"', "PROVIDER_INVALID_RESPONSE"),
    (b'[]', "PROVIDER_INVALID_RESPONSE"),
    (b' ' * (256 * 1024 + 1), "PROVIDER_INVALID_RESPONSE"),
    (b'{"model":"synthetic/chat-v1","extra":1e999}', "PROVIDER_INVALID_RESPONSE"),
    (b'{"model":"synthetic/chat-v1","model":"secret-upstream-body"}', "PROVIDER_INVALID_RESPONSE"),
    (_chat_response(model="secret-upstream-body"), "PROVIDER_MODEL_MISMATCH"),
    (_chat_response(object="chat.completion.chunk"), "PROVIDER_INVALID_RESPONSE"),
    (_chat_response(error={"message": "secret-upstream-body"}), "PROVIDER_INVALID_RESPONSE"),
    (_chat_response(choices=[]), "PROVIDER_INVALID_RESPONSE"),
    (_chat_response(choices=_chat_response()["choices"] * 2), "PROVIDER_INVALID_RESPONSE"),
], ids=["json", "utf8", "array", "response-bytes", "overflow", "duplicate-key", "model", "stream",
        "error-envelope", "no-choices", "two-choices"])
def test_chat_rejects_invalid_envelopes_with_safe_error(raw, code):
    transport = FakeTransport(raw)
    with pytest.raises(ProviderError) as exc:
        ChatAdapter(transport, _CHAT_MODEL).complete(_MESSAGES)
    assert exc.value.code == str(exc.value) == code
    assert len(transport.requests) == 1


@pytest.mark.parametrize(("choice_fields", "message_fields", "code"), [
    ({"index": False}, {}, "PROVIDER_INVALID_RESPONSE"),
    ({"finish_reason": "length"}, {}, "PROVIDER_GENERATION_INCOMPLETE"),
    ({"finish_reason": "content_filter"}, {}, "PROVIDER_GENERATION_INCOMPLETE"),
    ({"finish_reason": "tool_calls"}, {}, "PROVIDER_GENERATION_INCOMPLETE"),
    ({}, {"tool_calls": [{"name": "unused"}]}, "PROVIDER_GENERATION_INCOMPLETE"),
    ({}, {"function_call": {}}, "PROVIDER_GENERATION_INCOMPLETE"),
    ({}, {"refusal": "upstream-only"}, "PROVIDER_GENERATION_INCOMPLETE"),
    ({}, {"role": "user"}, "PROVIDER_INVALID_RESPONSE"),
    ({}, {"content": " \n"}, "PROVIDER_INVALID_RESPONSE"),
    ({}, {"content": "字" * 6000}, "PROVIDER_INVALID_RESPONSE"),
    ({}, {"content": [{"type": "text", "text": "upstream-only"}]}, "PROVIDER_INVALID_RESPONSE"),
], ids=["boolean-index", "truncation", "filter", "tool-finish", "tool-call", "function-call", "refusal",
        "role", "blank", "oversized-text", "structured-content"])
def test_chat_rejects_incomplete_or_non_text_output(choice_fields, message_fields, code):
    response = _chat_response()
    response["choices"][0].update(choice_fields)
    response["choices"][0]["message"].update(message_fields)
    with pytest.raises(ProviderError, match=f"^{code}$"):
        ChatAdapter(FakeTransport(response), _CHAT_MODEL).complete(_MESSAGES)


@pytest.mark.parametrize("usage", [
    None, {"prompt_tokens": 5}, {"prompt_tokens": True, "completion_tokens": 1, "total_tokens": 2},
    {"prompt_tokens": -1, "completion_tokens": 1, "total_tokens": 0},
    {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 3},
])
def test_chat_unavailable_usage_remains_unknown(usage):
    transport = FakeTransport(_chat_response(usage=usage))
    assert ChatAdapter(transport, _CHAT_MODEL).complete(_MESSAGES)["usage"] is None


def test_chat_transport_failure_is_propagated_without_retry():
    error = ProviderError("PROVIDER_TRANSPORT_TIMEOUT")
    transport = FakeTransport(error)
    with pytest.raises(ProviderError) as exc:
        ChatAdapter(transport, _CHAT_MODEL).complete(_MESSAGES)
    assert exc.value is error
    assert len(transport.requests) == 1


def test_bge_sends_float_encoding_and_reorders_to_input_order():
    model = "Pro/BAAI/bge-m3"
    transport = FakeTransport(_embedding_response(model=model, data=[
        {"object": "embedding", "index": 1, "embedding": _vector(-0.5)},
        {"object": "embedding", "index": 0, "embedding": _vector(2)},
    ], usage={"prompt_tokens": 3, "completion_tokens": 0, "total_tokens": 3, "extra": "upstream-only"}))
    result = BgeM3Adapter(transport, model).embed(["一", "二"])
    assert result == {
        "model": model, "vectors": [_vector(2.0), _vector(-0.5)],
        "usage": {"prompt_tokens": 3, "completion_tokens": 0, "total_tokens": 3},
    }
    assert all(type(value) is float for vector in result["vectors"] for value in vector)
    assert transport.requests == [("POST", "/v1/embeddings", {
        "model": model, "input": ["一", "二"], "encoding_format": "float",
    }, "api", "application/json")]


@pytest.mark.parametrize("inputs", [
    "a", [], ["a"] * 33, [True], [" "], ["\ud800"], ["字" * 3000], ["a" * 7000] * 5,
], ids=["not-list", "empty", "too-many", "not-text", "whitespace", "invalid-utf8", "text-bytes", "request-bytes"])
def test_bge_invalid_input_never_reaches_transport(inputs):
    transport = FakeTransport(b"")
    with pytest.raises(ProviderError, match=r"^PROVIDER_INVALID_INPUT$"):
        BgeM3Adapter(transport).embed(inputs)
    assert transport.requests == []


def test_bge_rejects_other_model_family_before_transport():
    transport = FakeTransport(b"")
    with pytest.raises(ProviderError, match=r"^PROVIDER_INVALID_INPUT$"):
        BgeM3Adapter(transport, "BAAI/bge-large-zh-v1.5")
    assert transport.requests == []


@pytest.mark.parametrize(("indices", "inputs"), [
    ([], ["a"]), ([0, 1], ["a"]), ([0, 0], ["a", "b"]), ([0, 2], ["a", "b"]), ([-1], ["a"]),
    ([False], ["a"]), ([0.0], ["a"]), ([None], ["a"]),
], ids=["missing-row", "extra-row", "duplicate", "out-of-range", "negative", "boolean", "float", "missing-index"])
def test_bge_rejects_bad_cardinality_and_indices(indices, inputs):
    transport = FakeTransport(_embedding_response(data=[
        {"index": index, "embedding": _vector()} for index in indices
    ]))
    with pytest.raises(ProviderError, match=r"^PROVIDER_INVALID_RESPONSE$"):
        BgeM3Adapter(transport).embed(inputs)


@pytest.mark.parametrize("vector", [
    "base64", [1] * 1023, [1] * 1025, _vector(0), _vector(True), _vector("1"),
    _vector(float("nan")), _vector(float("inf")), _vector(10**400),
], ids=["base64", "too-short", "too-long", "zero", "boolean", "string", "nan", "infinite", "float-overflow"])
def test_bge_rejects_invalid_vectors(vector):
    transport = FakeTransport(_embedding_response(data=[{"index": 0, "embedding": vector}]))
    with pytest.raises(ProviderError, match=r"^PROVIDER_INVALID_RESPONSE$"):
        BgeM3Adapter(transport).embed(["a"])


def test_bge_requires_exact_requested_model():
    transport = FakeTransport(_embedding_response(model="Pro/BAAI/bge-m3"))
    with pytest.raises(ProviderError, match=r"^PROVIDER_MODEL_MISMATCH$"):
        BgeM3Adapter(transport).embed(["a"])


@pytest.mark.parametrize(("usage", "expected"), [
    ({"prompt_tokens": 3, "total_tokens": 3}, {"prompt_tokens": 3, "total_tokens": 3}),
    (None, None), ({"prompt_tokens": 3}, None), ({"prompt_tokens": 3, "total_tokens": 4}, None),
    ({"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4}, None),
    ({"prompt_tokens": 3, "completion_tokens": False, "total_tokens": 3}, None),
])
def test_bge_usage_accounts_for_only_valid_reported_prompt_tokens(usage, expected):
    transport = FakeTransport(_embedding_response(usage=usage))
    assert BgeM3Adapter(transport).embed(["a"])["usage"] == expected
