"""Prepare the local database: create tables, load demo fixtures, print what is where.

Usage:
    uv run python scripts/init_db.py              # uses DATABASE_URL from .env
    uv run python scripts/init_db.py --url sqlite:///./data/x.db
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from shopping_backend.core.config import Settings  # noqa: E402
from shopping_backend.db.base import create_all  # noqa: E402
from shopping_backend.db.engine import create_engine_from_url, describe_engine  # noqa: E402
from shopping_backend.seeds.loader import count_rows, load_seeds  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Initialise the shopping-backend database.")
    parser.add_argument("--url", default=None, help="override DATABASE_URL")
    parser.add_argument("--no-seed", action="store_true", help="create tables only")
    parser.add_argument("--reset", action="store_true", default=True, help="clear fixture tables first (default)")
    args = parser.parse_args(argv)

    settings = Settings()
    url = args.url or settings.resolved_database_url
    engine = create_engine_from_url(url)
    create_all(engine)

    report: dict[str, object] = {"engine": describe_engine(engine), "rows": count_rows(engine)}
    if not args.no_seed:
        loaded = load_seeds(engine, reset=args.reset)
        report["loaded"] = loaded.as_dict()
        report["rows"] = count_rows(engine)

    print(json.dumps(report, ensure_ascii=False, indent=2))
    print("\nNext: uv run python scripts/run_demo.py   (starts both services and runs the 7 checks)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
