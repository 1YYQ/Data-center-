"""NTFS aliases must not escape the explicit backup or receipt boundary."""
import json
import os
from pathlib import Path
import subprocess

import pytest

from . import backup, backup_schedule, pilot


@pytest.mark.skipif(os.name != "nt", reason="Windows device namespace")
def test_extended_alias_is_rejected_and_identity_overlap_is_detected(tmp_path):
    alias = Path("\\\\?\\" + str(tmp_path))
    assert alias.samefile(tmp_path)
    with pytest.raises(backup.BackupRefused, match="namespace"):
        backup._plain_path(alias)
    with pytest.raises(backup.BackupRefused, match="nonoverlapping"):
        backup._disjoint(tmp_path, alias / "new-backup")


@pytest.mark.skipif(os.name != "nt", reason="Windows junction boundary")
def test_scheduler_refuses_receipt_junction_before_any_write(tmp_path, monkeypatch):
    live = tmp_path / "live"
    live.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    config = live / "pilot.json"
    config.write_text(json.dumps({"schema": "dc-pilot-runtime/1", "synthetic_pilot": True}), encoding="utf-8")
    monkeypatch.setattr(pilot, "LIVE_ROOT", live)
    junction = live / "backup-receipts"
    made = subprocess.run(["cmd.exe", "/c", "mklink", "/J", str(junction), str(outside)],
                          capture_output=True, check=False)
    assert made.returncode == 0, "The isolated test junction could not be created"
    try:
        with pytest.raises(backup.BackupRefused, match="reparse"):
            backup_schedule._locations(config)
        assert not list(outside.iterdir())
    finally:
        # Remove only the exact test junction itself, never its target directory.
        assert junction.lstat().st_file_attributes & 0x400
        os.rmdir(junction)
