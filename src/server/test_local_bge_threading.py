from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from threading import get_native_id
from types import SimpleNamespace

import pytest

from . import local_bge
from .provider_transport import ProviderError


@pytest.fixture
def local_model(tmp_path, monkeypatch):
    state = SimpleNamespace(calls=[], inputs=[], tokens=2, revision=1, change_snapshot=False,
                            vector=[1.0] + [0.0] * (local_bge.DIMENSIONS - 1))

    def snapshot(directory):
        assert directory == tmp_path
        state.calls.append(("snapshot", get_native_id()))
        return (state.revision,)

    def load(directory, stats):
        assert directory == tmp_path and stats == (state.revision,)
        state.calls.append(("load", get_native_id()))
        return model

    def processor(texts, *, add_special_tokens, padding, truncation, return_length):
        assert add_special_tokens and not padding and not truncation and return_length
        state.calls.append(("tokenize", get_native_id()))
        state.inputs.append(texts)
        return {"length": [state.tokens] * len(texts)}

    def encode(texts, *, batch_size, convert_to_numpy, normalize_embeddings, show_progress_bar):
        assert batch_size == 4 and convert_to_numpy and not normalize_embeddings and not show_progress_bar
        state.calls.append(("encode", get_native_id()))
        if state.change_snapshot:
            state.revision += 1
        return SimpleNamespace(tolist=lambda: [state.vector] * len(texts))

    model = SimpleNamespace(processor=processor, encode=encode)
    monkeypatch.setattr(local_bge, "_model_files", snapshot)
    monkeypatch.setattr(local_bge, "_load", load)
    return local_bge.LocalBgeEndpoint(tmp_path), state


def test_preload_and_encode_reuse_one_inference_thread(local_model):
    endpoint, state = local_model
    texts = ["synthetic input"]

    def call(operation, *args):
        return get_native_id(), operation(*args)

    with ExitStack() as stack:
        # Keep separate one-shot callers alive until all native thread IDs are observed.
        callers = [stack.enter_context(ThreadPoolExecutor(max_workers=1)) for _ in range(3)]
        preload_caller, preloaded = callers[0].submit(call, endpoint.preload).result(timeout=5)
        first_caller, first = callers[1].submit(call, endpoint.embed, texts).result(timeout=5)
        second_caller, second = callers[2].submit(call, endpoint.embed, texts).result(timeout=5)
        caller_ids = {preload_caller, first_caller, second_caller}
        model_ids = {thread_id for _, thread_id in state.calls}
        assert len(caller_ids) == 3 and len(model_ids) == 1 and model_ids.isdisjoint(caller_ids)

    assert preloaded is None
    assert first == second == {"model": local_bge.MODEL, "vectors": [state.vector], "usage": None}
    assert [operation for operation, _ in state.calls] == ["snapshot", "load"] + [
        "snapshot", "load", "tokenize", "encode", "snapshot"] * 2
    assert state.inputs == [texts, texts] and all(inputs is not texts for inputs in state.inputs)


def test_embedding_rechecks_snapshot_after_encode(local_model):
    endpoint, state = local_model
    state.change_snapshot = True

    with pytest.raises(ProviderError, match="^LOCAL_BGE_SNAPSHOT_CHANGED$"):
        endpoint.embed(["synthetic input"])

    state.change_snapshot = False
    assert endpoint.embed(["synthetic input"])["vectors"] == [state.vector]


def test_embedding_rejects_invalid_input_tokens_and_vectors(local_model):
    endpoint, state = local_model
    for texts in ([], ["input"] * 33, [" "], ["x" * (local_bge._MAX_TEXT_BYTES + 1)]):
        with pytest.raises(ProviderError, match="^PROVIDER_INVALID_INPUT$"):
            endpoint.embed(texts)
    assert not state.calls

    state.tokens = local_bge._MAX_TOKENS + 1
    with pytest.raises(ProviderError, match="^PROVIDER_INVALID_INPUT$"):
        endpoint.embed(["synthetic input"])
    assert [operation for operation, _ in state.calls] == ["snapshot", "load", "tokenize"]

    state.tokens = 2
    for vector in ([1.0], [float("nan")] + state.vector[1:]):
        state.vector = vector
        with pytest.raises(ProviderError, match="^PROVIDER_INVALID_RESPONSE$"):
            endpoint.embed(["synthetic input"])
