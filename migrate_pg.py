#!/usr/bin/env python3
"""Migrate the SQLite massinvestor.db into Railway Postgres.

Connects via DATABASE_URL (or --host/--port/--user/--password/--database),
creates the firms table, and copies every row from SQLite. Idempotent:
drops + recreates the table. JSON columns are stored as JSONB.
"""
import argparse
import json
import os
import sqlite3

import pg8000.dbapi

SCHEMA = """
CREATE TABLE IF NOT EXISTS firms (
    name TEXT PRIMARY KEY,
    type_key TEXT,
    website TEXT,
    offices JSONB,
    stages JSONB,
    industries JSONB,
    description TEXT,
    team_json JSONB,
    funding_json JSONB,
    portfolio_json JSONB,
    news_json JSONB,
    crawled_at TEXT
);
"""


def parse_database_url(url):
    """postgresql://user:pass@host:port/db -> dict for pg8000."""
    from urllib.parse import urlparse, unquote
    u = urlparse(url)
    return {
        "host": u.hostname,
        "port": u.port or 5432,
        "user": unquote(u.username) if u.username else "postgres",
        "password": unquote(u.password) if u.password else "",
        "database": (u.path or "/railway").lstrip("/"),
    }


def to_json_or_none(v):
    """Return the raw JSON string (or None) so Postgres parses it via ::jsonb.

    pg8000 would otherwise re-serialize Python objects ambiguously; passing the
    literal JSON text is safest for the ::jsonb cast.
    """
    if not v:
        return None
    try:
        json.loads(v)  # validate
        return v
    except (ValueError, TypeError):
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sqlite", default="massinvestor.db")
    ap.add_argument("--url", default=os.environ.get("DATABASE_URL", ""),
                    help="Postgres URL. If empty, use --host etc.")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=5432)
    ap.add_argument("--user", default="postgres")
    ap.add_argument("--password", default="")
    ap.add_argument("--database", default="railway")
    args = ap.parse_args()

    if args.url:
        cfg = parse_database_url(args.url)
    else:
        cfg = {"host": args.host, "port": args.port, "user": args.user,
               "password": args.password, "database": args.database}

    sqlite = sqlite3.connect(args.sqlite)
    sqlite.row_factory = sqlite3.Row
    rows = sqlite.execute("SELECT * FROM firms").fetchall()
    print(f"Read {len(rows)} rows from {args.sqlite}")

    pg = pg8000.dbapi.connect(**cfg)
    cur = pg.cursor()
    cur.execute("DROP TABLE IF EXISTS firms")
    cur.execute(SCHEMA)
    pg.commit()

    sql = (
        "INSERT INTO firms (name, type_key, website, offices, stages, industries,"
        " description, team_json, funding_json, portfolio_json, news_json, crawled_at)"
        " VALUES (%s, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb, %s, %s::jsonb,"
        " %s::jsonb, %s::jsonb, %s::jsonb, %s)"
    )
    batch = []
    for r in rows:
        batch.append((
            r["name"], r["type_key"], r["website"],
            to_json_or_none(r["offices"]),
            to_json_or_none(r["stages"]),
            to_json_or_none(r["industries"]),
            r["description"],
            to_json_or_none(r["team_json"]),
            to_json_or_none(r["funding_json"]),
            to_json_or_none(r["portfolio_json"]),
            to_json_or_none(r["news_json"]),
            r["crawled_at"],
        ))
    cur.executemany(sql, batch)
    pg.commit()
    print(f"Inserted {len(batch)} rows")

    cur.execute("SELECT COUNT(*) FROM firms")
    print("Postgres firms:", cur.fetchone()[0])
    cur.execute("CREATE INDEX IF NOT EXISTS idx_firms_name ON firms(name)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_firms_type ON firms(type_key)")
    pg.commit()
    print("Indexes created.")
    pg.close()
    sqlite.close()


if __name__ == "__main__":
    main()
