"""Apply orchestration database migrations."""

from __future__ import annotations

import argparse
import os
from getpass import getpass

from orchestration.postgres import PostgresMigrationRunner


def main() -> None:
    parser = argparse.ArgumentParser(description="Apply Hermes orchestration migrations")
    parser.add_argument(
        "--database-url",
        default=os.getenv("HERMES_ORCHESTRATION_DATABASE_URL", ""),
        help="Postgres database URL. Defaults to HERMES_ORCHESTRATION_DATABASE_URL.",
    )
    parser.add_argument(
        "--prompt",
        action="store_true",
        help="Prompt for the database URL without echoing it.",
    )
    args = parser.parse_args()

    database_url = args.database_url
    if args.prompt:
        database_url = getpass("Database URL: ")
    if not database_url:
        raise SystemExit("Missing database URL")

    applied = PostgresMigrationRunner(database_url).apply()
    if applied:
        print("Applied migrations:")
        for item in applied:
            print(f"- {item}")
    else:
        print("No pending migrations.")


if __name__ == "__main__":
    main()
