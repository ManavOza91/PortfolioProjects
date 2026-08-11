"""Command-line entry point.

`./run.sh` with no arguments is the normal path: migrate, seed, rescore, serve.
"""

from __future__ import annotations

import argparse
import sys
import threading
import webbrowser
from pathlib import Path

from .config import PROJECT_ROOT, get_config, get_taxonomy


def _run_migrations() -> None:
    from alembic import command
    from alembic.config import Config as AlembicConfig

    cfg = AlembicConfig(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    command.upgrade(cfg, "head")


def _ensure_ready() -> None:
    """Migrate and seed before any command that reads or writes data.

    Without this, a command run against a fresh database finds no signal_types and
    silently files every signal as unscored — the taxonomy is looked up by key, and
    an unknown key is indistinguishable from an unseeded table.
    """
    from .db import session_scope
    from .seed import seed_taxonomy

    _run_migrations()
    with session_scope() as session:
        seed_taxonomy(session)


def cmd_migrate(_args) -> int:
    _run_migrations()
    print("Database is up to date.")
    return 0


def cmd_seed(_args) -> int:
    from .db import session_scope
    from .seed import seed_taxonomy

    _run_migrations()

    with session_scope() as session:
        result = seed_taxonomy(session)
    print(f"Taxonomy: {result.summary()}.")
    for key in result.kept_for_history:
        print(f"  note: {key} was removed from the YAML but kept — signals reference it.")
    return 0


def cmd_rescore(_args) -> int:
    from .db import session_scope
    from .scoring import rescore_all

    _ensure_ready()

    with session_scope() as session:
        stats = rescore_all(session)
    parts = [f"{k.replace('tier_', 'Tier ')}: {v}" for k, v in stats.items() if k != "companies"]
    print(f"Rescored {stats['companies']} companies. " + ", ".join(parts))
    return 0


def cmd_import(args) -> int:
    from .db import session_scope
    from .importer import run_import

    _ensure_ready()

    with session_scope() as session:
        plan = run_import(session, dry_run=args.dry_run)
        print(plan.report())
        if args.dry_run:
            # run_import already rolled back; make sure nothing sneaks through.
            session.rollback()
    return 0


def cmd_purge_contacts(args) -> int:
    from .db import session_scope
    from .ingest import purge_personal_data

    _run_migrations()

    if not args.yes:
        answer = input(
            "This deletes EVERY contact and all personal data. Scoring, the Buddy "
            "Score and reporting will keep working. Type 'delete' to confirm: "
        )
        if answer.strip().lower() != "delete":
            print("Cancelled — nothing was deleted.")
            return 1

    with session_scope() as session:
        result = purge_personal_data(session)
    print(
        f"Deleted {result.contacts_deleted} contacts. "
        f"{result.activity_rows_kept} activity records kept "
        f"({result.activity_rows_detached} detached from a contact). "
        "Scoring and reporting are unaffected."
    )
    return 0


def cmd_demo(_args) -> int:
    from .demo import load_demo

    _ensure_ready()

    from .db import session_scope

    with session_scope() as session:
        summary = load_demo(session)
    print(summary)
    return 0


def cmd_test(_args) -> int:
    import subprocess

    return subprocess.call([sys.executable, "-m", "pytest", "-q"], cwd=str(PROJECT_ROOT))


def cmd_serve(args) -> int:
    import uvicorn

    from .db import session_scope
    from .scoring import rescore_all
    from .seed import seed_taxonomy

    cfg = get_config()

    _run_migrations()
    with session_scope() as session:
        seed_result = seed_taxonomy(session)
    with session_scope() as session:
        rescore_all(session)

    host = args.host or cfg.server.get("host", "127.0.0.1")
    port = args.port or int(cfg.server.get("port", 8420))
    url = f"http://{host}:{port}"

    taxonomy = get_taxonomy()
    print()
    print(f"  Signal Engine — {len(taxonomy.types)} signal types loaded ({seed_result.summary()})")
    print(f"  Open {url}")
    if not cfg.llm_enabled:
        print("  No ANTHROPIC_API_KEY — note parsing is off; notes are saved raw.")
    print("  Ctrl-C to stop.")
    print()

    if cfg.server.get("open_browser", True) and not args.no_browser:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()

    uvicorn.run("signal_engine.web.app:app", host=host, port=port, log_level="warning")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="signal-engine",
        description="Signal-driven outbound engine. Run with no command to start the app.",
    )
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("serve", help="migrate, seed, rescore, and start the web app")
    p.add_argument("--host")
    p.add_argument("--port", type=int)
    p.add_argument("--no-browser", action="store_true")
    p.set_defaults(func=cmd_serve)

    sub.add_parser("migrate", help="apply database migrations").set_defaults(func=cmd_migrate)
    sub.add_parser("seed", help="reload data/taxonomy.yaml").set_defaults(func=cmd_seed)
    sub.add_parser("rescore", help="recompute all company scores").set_defaults(func=cmd_rescore)
    sub.add_parser("demo", help="load a small worked example").set_defaults(func=cmd_demo)
    sub.add_parser("test", help="run the test suite").set_defaults(func=cmd_test)

    p = sub.add_parser("import", help="import companies and contacts from data/import/*.csv")
    p.add_argument("--dry-run", action="store_true", help="preview only, write nothing")
    p.set_defaults(func=cmd_import)

    p = sub.add_parser("purge-contacts", help="delete ALL personal data, keeping scoring intact")
    p.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    p.set_defaults(func=cmd_purge_contacts)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        args = parser.parse_args(["serve", *(argv or [])])
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        print("\nStopped.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
