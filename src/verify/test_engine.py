"""L1 regression checks for the verifier's evidence and verdict contract."""

from __future__ import annotations

import json
from pathlib import Path
import sys
import shutil
import unittest
from uuid import uuid4

try:
    from .engine import VerifyError, verify_task
except ImportError:  # unittest discover imports this file as a top-level module.
    from engine import VerifyError, verify_task


class VerifyEngineTests(unittest.TestCase):
    def make_case(self, command, assertions=None):
        root = Path(__file__).resolve().parents[2] / ".verify" / "test-fixtures" / uuid4().hex
        root.mkdir(parents=True)
        self.fixture_root = root
        (root / "AGENTS.md").write_text("governance\n", encoding="utf-8")
        (root / "spec.md").write_text("acceptance\n", encoding="utf-8")
        (root / "plan.md").write_text("scope\n", encoding="utf-8")
        (root / "contract.md").write_text("contract\n", encoding="utf-8")
        (root / "task.json").write_text(json.dumps({
            "task": {"id": "test", "revision": "1"},
            "sources": {"governance": ["AGENTS.md"], "spec": ["spec.md"], "plan": ["plan.md"], "contract": ["contract.md"]},
            "checks": [{"id": "command", "command": command, "expected_exit": 0, "assertions": assertions or []}],
            "acceptance": [{"id": "A1", "statement": "command is evidenced", "checks": ["command"], "evidence": ["AGENTS.md"]}],
        }), encoding="utf-8")
        return root

    def tearDown(self):
        if getattr(self, "fixture_root", None):
            shutil.rmtree(self.fixture_root, ignore_errors=True)

    def test_command_and_json_assertion_pass(self):
        root = self.make_case([sys.executable, "-c", "print('ok')"])
        report = verify_task(root, "task.json")
        self.assertEqual(report["verdict"], "PASS")
        self.assertEqual(report["checks"][0]["exit_code"], 0)

    def test_assertion_failure_is_fail(self):
        root = self.make_case([sys.executable, "-c", "pass"], [{"file": "result.json", "path": "ok", "equals": True}])
        (root / "result.json").write_text('{"ok": false}\n', encoding="utf-8")
        report = verify_task(root, "task.json")
        self.assertEqual(report["verdict"], "FAIL")

    def test_missing_source_blocks(self):
        root = self.make_case([sys.executable, "-c", "pass"])
        config = json.loads((root / "task.json").read_text(encoding="utf-8"))
        config["sources"]["spec"] = ["does-not-exist.md"]
        (root / "task.json").write_text(json.dumps(config), encoding="utf-8")
        report = verify_task(root, "task.json")
        self.assertEqual(report["verdict"], "BLOCKED")

    def test_path_escape_is_rejected(self):
        root = self.make_case([sys.executable, "-c", "pass"])
        config = json.loads((root / "task.json").read_text(encoding="utf-8"))
        config["sources"]["spec"] = ["../outside.md"]
        (root / "task.json").write_text(json.dumps(config), encoding="utf-8")
        with self.assertRaises(VerifyError):
            verify_task(root, "task.json")


if __name__ == "__main__":
    unittest.main()
