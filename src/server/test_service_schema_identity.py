"""A reviewed migration plan must bind the live table definitions it imports."""
from pathlib import Path
import shutil
import pytest
from . import migration_safety as safety


@pytest.mark.parametrize("module", ["service_models.py", "service_use_models.py", "parse_jobs.py", "embedding_jobs.py"])
def test_service_schema_edit_invalidates_source_identity(tmp_path, monkeypatch, module):
    root = Path(safety.ROOT)
    target = tmp_path / "src/server"
    target.mkdir(parents=True)
    for path in (root / "src/server").glob("*.py"):
        shutil.copyfile(path, target / path.name)
    shutil.copytree(root / "src/server/migrations", target / "migrations", ignore=shutil.ignore_patterns("__pycache__"))
    monkeypatch.setattr(safety, "ROOT", tmp_path)
    reviewed = safety._schema_source()
    assert safety._schema_source() == reviewed
    changed = target / module
    changed.write_bytes(changed.read_bytes() + b"\n# SYNTHETIC schema candidate changes after review\n")
    assert safety._schema_source() != reviewed
