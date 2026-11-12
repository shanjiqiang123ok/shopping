"""Delete the local SQLite files this project created. Refuses to run without --yes.

`data/` is project-owned scratch state (business fixtures + checkpoint snapshots), so this is the
supported way to start clean — but it still asks for confirmation because a long checkpoint file is
someone's conversation history.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from shopping_backend.core.config import Settings  # noqa: E402

SUFFIXES = ("", "-shm", "-wal", "-journal")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Remove local SQLite data files.")
    parser.add_argument("--yes", action="store_true", help="actually delete (otherwise dry-run)")
    parser.add_argument("--checkpoints-only", action="store_true")
    parser.add_argument(
        "--keep-checkpoint-threads", action="store_true", help="truncate threads instead of deleting the file"
    )
    args = parser.parse_args(argv)

    settings = Settings()
    targets: list[Path] = []

    if not args.checkpoints_only:
        from sqlalchemy.engine import make_url

        url = make_url(settings.resolved_database_url)
        if url.database and url.database != ":memory:":
            business = Path(url.database)
            targets.extend(Path(str(business) + suffix) for suffix in SUFFIXES)

    checkpoint = settings.resolved_checkpoint_db_path
    targets.extend(Path(str(checkpoint) + suffix) for suffix in SUFFIXES)

    existing = [path for path in targets if path.exists()]
    report = {"would_delete": [str(path) for path in existing], "data_dir": str(settings.data_dir)}

    if not args.yes:
        report["mode"] = "dry-run"
        print(json.dumps(report, ensure_ascii=False, indent=2))
        print("\nRe-run with --yes to delete.")
        return 0

    if args.keep_checkpoint_threads and checkpoint.exists():
        import asyncio

        from shopping_backend.graph.checkpoint import open_checkpoint_provider

        async def _truncate() -> int:
            provider = await open_checkpoint_provider(settings.model_copy(update={"CHECKPOINT_BACKEND": "sqlite"}))
            removed = 0
            try:
                import aiosqlite

                async with aiosqlite.connect(str(checkpoint)) as conn:
                    cursor = await conn.execute("SELECT DISTINCT thread_id FROM checkpoints")
                    threads = [row[0] for row in await cursor.fetchall()]
                for thread_id in threads:
                    if await provider.delete_thread(thread_id):
                        removed += 1
            finally:
                await provider.aclose()
            return removed

        report["threads_cleared"] = asyncio.run(_truncate())
        report["mode"] = "threads-cleared"
    else:
        for path in existing:
            path.unlink()
        report["deleted"] = len(existing)
        report["mode"] = "deleted"

    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
