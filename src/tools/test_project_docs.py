"""Discriminating failure cases for the offline document gate."""

from pathlib import Path

import pytest

from src.tools.project_docs import anchors, check_links, task_graph


@pytest.mark.parametrize(
    ("text", "failure"),
    [
        (
            "- [ ] T001 one\n| T001 | T999 | evidence |\n",
            "unknown dependency",
        ),
        (
            "- [ ] T001 one\n- [ ] T002 two\n"
            "| T001 | T002 | evidence |\n| T002 | T001 | evidence |\n",
            "dependency cycle",
        ),
        (
            "- [ ] T001 one\n| T001 | 无 | evidence |\n"
            "- [ ] **T001-PDF-01｜child**\n  - 运行边界：合成。\n",
            "missing explicit dependency",
        ),
    ],
)
def test_invalid_task_graph_is_rejected(text: str, failure: str) -> None:
    _, errors = task_graph(text)
    assert any(failure in error for error in errors)


def test_links_distinguish_broken_contract_from_uncertified_evidence(tmp_path: Path) -> None:
    source, target = tmp_path / "README.md", tmp_path / "spec.md"
    target.write_text("# 1.3 工作：范围\n", encoding="utf-8")
    source.write_text(
        "[valid](spec.md#13-工作范围)\n"
        "[bad heading](spec.md#missing)\n"
        "[private evidence](outputs/not-present/report.json)\n"
        "```text\n[example](fake.md)\n```\n",
        encoding="utf-8",
    )
    errors, warnings, checked = check_links(tmp_path, [source])
    assert checked == 2
    assert len(errors) == 1 and "missing heading" in errors[0]
    assert len(warnings) == 1 and "not certified" in warnings[0]
    assert "13-工作范围" in anchors(target.read_text(encoding="utf-8"))
