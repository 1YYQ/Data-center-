"""Fixed synthetic egress remains exact while supporting accepted follow-ups."""
from types import SimpleNamespace
from uuid import uuid4

import pytest

from .schemas import QueryInput
from .testing_service_use import _fixture_inputs_match


def fixture_task():
    payload = QueryInput(query="SYNTHETIC fixed question", model="SYNTHETIC-1",
        knowledge_base_ids=[str(uuid4())], mode="answer", service_mode="selected_service").model_dump()
    task = SimpleNamespace(input_snapshot=dict(payload), effective_input=dict(payload), conversation_id=str(uuid4()))
    return task, [payload, {**payload, "knowledge_base_ids": []}], [payload]


def test_fixed_fixture_first_input_response_and_follow_up_keep_exact_business_input():
    task, inputs, effective = fixture_task()
    assert _fixture_inputs_match(task, inputs, effective)
    task.input_snapshot["model"] = None
    assert _fixture_inputs_match(task, inputs, effective)  # resolved on accepted task
    for payload in (task.input_snapshot, task.effective_input):
        payload.update(conversation_id=task.conversation_id, context_revision=1)
    assert _fixture_inputs_match(task, inputs, effective)
    task.input_snapshot["knowledge_base_ids"] = []
    assert _fixture_inputs_match(task, inputs, effective)


@pytest.mark.parametrize("field,value", [
    ("query", "unapproved question"), ("model", "unknown model"),
    ("knowledge_base_ids", ["unapproved-library"]), ("service_mode", "local"),
    ("mode", "retrieve"), ("level", "secret"), ("unexpected", "content"),
])
def test_fixed_fixture_never_normalizes_other_business_fields(field, value):
    task, inputs, effective = fixture_task()
    task.input_snapshot[field] = value
    assert not _fixture_inputs_match(task, inputs, effective)
    task.input_snapshot = dict(inputs[0])
    task.effective_input[field] = value
    assert not _fixture_inputs_match(task, inputs, effective)


@pytest.mark.parametrize("conversation,revision", [(None, 1), ("unknown", 1), (str(uuid4()), 1), ("self", True), ("self", -1)])
def test_fixed_fixture_refuses_unbound_or_invalid_conversation_metadata(conversation, revision):
    task, inputs, effective = fixture_task()
    for payload in (task.input_snapshot, task.effective_input):
        payload.update(conversation_id=task.conversation_id if conversation == "self" else conversation, context_revision=revision)
    assert not _fixture_inputs_match(task, inputs, effective)
