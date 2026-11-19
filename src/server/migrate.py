"""Local maintenance CLI: plan first, then apply that exact target/pre-state."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from .config import Settings
from .db import build_engine
from .migration_safety import MigrationEvidenceError, MigrationRefused, apply_local_migrations, maintenance_plan


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("plan", "apply"))
    parser.add_argument("--url-env", required=True, help="Environment variable containing the explicit local database URL")
    parser.add_argument("--plan", required=True, type=Path, help="Reviewable plan file (must not exist when planning)")
    parser.add_argument("--storage-dir", type=Path)
    parser.add_argument("--evidence-dir", type=Path)
    args = parser.parse_args()
    url = os.environ.get(args.url_env)
    if not url:
        parser.error("The selected URL environment variable is empty; there is no default target.")
    engine = None
    try:
        if args.action == "plan":
            plan = maintenance_plan(url)
            args.plan.parent.mkdir(parents=True, exist_ok=True)
            with args.plan.open("x", encoding="utf-8") as handle:
                json.dump(plan, handle, indent=2)
            print(json.dumps({"status": "PLANNED", "plan": str(args.plan.resolve()), **plan}))
        else:
            if args.storage_dir is None or args.evidence_dir is None:
                parser.error("apply requires --storage-dir and --evidence-dir")
            plan = json.loads(args.plan.read_text(encoding="utf-8"))
            # Validation occurs before engine construction or any DDL.
            if maintenance_plan(url) != plan:
                raise MigrationRefused("Target/pre-state/source differs from the reviewed plan.")
            engine = build_engine(url)
            result = apply_local_migrations(engine, Settings(database_url=url, storage_dir=args.storage_dir),
                                            evidence_dir=args.evidence_dir, plan=plan)
            print(json.dumps({"status": result["status"], "receipt": result["receipt_path"]}))
    except MigrationEvidenceError as error:
        print(json.dumps({"status": error.status, "receipt": error.receipt_path,
                          "action": "Inspect current schema and receipt; do not blindly reapply."}))
        return 3
    except (MigrationRefused, ValueError, OSError) as error:
        print(json.dumps({"status": "REFUSED", "reason": str(error)}))
        return 2
    finally:
        if engine is not None:
            engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
