"""The advertised SQLite install must not import the optional pgvector extra."""
import subprocess
import sys


def test_sqlite_startup_and_migrations_without_pgvector(tmp_path):
    script = r'''
import builtins
import sys
from pathlib import Path
real_import = builtins.__import__
def without_pgvector(name, *args, **kwargs):
    if name == "pgvector" or name.startswith("pgvector."):
        raise ModuleNotFoundError("deliberately unavailable optional pgvector", name="pgvector")
    return real_import(name, *args, **kwargs)
builtins.__import__ = without_pgvector
from src.server.app import create_app
from src.server.config import Settings
from src.server.db import build_engine, init_db
root = Path(sys.argv[1])
settings = Settings(database_url="sqlite:///" + (root / "isolated.db").as_posix(), storage_dir=root / "private")
engine = build_engine(settings.database_url)
try:
    init_db(engine, settings)
    app = create_app(settings)
    assert app.openapi()["paths"]["/knowledge/queries"]
    app.state.engine.dispose()
finally:
    engine.dispose()
'''
    result = subprocess.run([sys.executable, "-c", script, str(tmp_path)], capture_output=True, text=True, timeout=40)
    assert result.returncode == 0, result.stdout + result.stderr
