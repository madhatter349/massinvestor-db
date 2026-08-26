#!/usr/bin/env python3
"""Flask dashboard for the Massinvestor public directory database."""
import json
import os
import re
import sqlite3

from flask import Flask, abort, g, render_template, request

DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(__file__), "massinvestor.db"))

app = Flask(__name__)

TYPE_LABELS = {
    "VC": "Venture Capital", "PE": "Private Equity", "A": "Angel",
    "I": "Incubator", "MB": "Merchant Bank", "VD": "Venture Debt",
    "FI": "Family Investment Office", "FOF": "Fund of Funds",
    "ED": "Economic Development Office", "TT": "Technology Transfer Office",
    "CVC": "Corporate Venture Capital", "SEC": "Secondary Purchaser",
    "HF": "Hedge Fund/Mutual Fund",
}


def get_db():
    db = getattr(g, "_db", None)
    if db is None:
        db = g._db = sqlite3.connect(DB_PATH)
        db.row_factory = sqlite3.Row
    return db


@app.teardown_appcontext
def close_db(exc):
    db = getattr(g, "_db", None)
    if db is not None:
        db.close()


def parse_json(field):
    if not field:
        return []
    try:
        return json.loads(field)
    except (ValueError, TypeError):
        return []


def parse_amount(s):
    if not s:
        return 0.0
    m = re.search(r"\$?([\d,\.]+)\s*([KMB]|million|billion)?", s, re.I)
    if not m:
        return 0.0
    val = float(m.group(1).replace(",", ""))
    unit = (m.group(2) or "").lower()
    mult = {"k": 1e3, "m": 1e6, "b": 1e9, "million": 1e6, "billion": 1e9}
    return val * mult.get(unit, 1.0)


@app.template_filter("json_load")
def json_load_filter(s):
    return parse_json(s)


@app.template_filter("money")
def money_filter(v):
    if v >= 1e9:
        return f"${v/1e9:.2f}B"
    if v >= 1e6:
        return f"${v/1e6:.1f}M"
    if v >= 1e3:
        return f"${v/1e3:.1f}K"
    return f"${v:,.0f}"


def firm_to_dict(row):
    return {
        "name": row["name"],
        "type_key": row["type_key"],
        "type_label": TYPE_LABELS.get(row["type_key"], row["type_key"] or "N/A"),
        "website": row["website"],
        "offices": parse_json(row["offices"]),
        "stages": parse_json(row["stages"]),
        "industries": parse_json(row["industries"]),
        "description": row["description"],
        "team": parse_json(row["team_json"]),
        "funding": parse_json(row["funding_json"]),
        "portfolio": parse_json(row["portfolio_json"]),
        "news": parse_json(row["news_json"]),
        "crawled_at": row["crawled_at"],
    }


@app.route("/")
def index():
    db = get_db()
    total = db.execute("SELECT COUNT(*) FROM firms").fetchone()[0]
    types = db.execute(
        "SELECT type_key, COUNT(*) n FROM firms GROUP BY type_key ORDER BY n DESC"
    ).fetchall()
    states = {}
    for r in db.execute("SELECT offices FROM firms"):
        for off in parse_json(r["offices"]):
            m = re.search(r"\b([A-Z]{2})\b(?:$|\s)", off)
            if m:
                states[m.group(1)] = states.get(m.group(1), 0) + 1
    top_states = sorted(states.items(), key=lambda x: -x[1])[:10]
    with_team = db.execute(
        "SELECT COUNT(*) FROM firms WHERE team_json != '[]'"
    ).fetchone()[0]
    with_funding = db.execute(
        "SELECT COUNT(*) FROM firms WHERE funding_json != '[]'"
    ).fetchone()[0]
    with_portfolio = db.execute(
        "SELECT COUNT(*) FROM firms WHERE portfolio_json != '[]'"
    ).fetchone()[0]
    recent = db.execute(
        "SELECT * FROM firms ORDER BY crawled_at DESC LIMIT 8"
    ).fetchall()
    return render_template(
        "index.html",
        total=total,
        types=[{"key": r["type_key"], "label": TYPE_LABELS.get(r["type_key"], r["type_key"] or "N/A"), "n": r["n"]} for r in types],
        top_states=top_states,
        with_team=with_team,
        with_funding=with_funding,
        with_portfolio=with_portfolio,
        recent=[firm_to_dict(r) for r in recent],
    )


@app.route("/browse")
def browse():
    db = get_db()
    q = (request.args.get("q") or "").strip()
    page = max(1, int(request.args.get("page", 1)))
    per_page = 50
    where, params = "", []
    if q:
        where = "WHERE name LIKE ? OR description LIKE ? OR website LIKE ?"
        like = f"%{q}%"
        params = [like, like, like]
    total = db.execute(f"SELECT COUNT(*) FROM firms {where}", params).fetchone()[0]
    rows = db.execute(
        f"SELECT * FROM firms {where} ORDER BY name LIMIT ? OFFSET ?",
        params + [per_page, (page - 1) * per_page],
    ).fetchall()
    pages = max(1, (total + per_page - 1) // per_page)
    return render_template(
        "browse.html",
        firms=[firm_to_dict(r) for r in rows],
        q=q,
        page=page,
        pages=pages,
        total=total,
    )


@app.route("/firm/<path:name>")
def firm(name):
    db = get_db()
    row = db.execute("SELECT * FROM firms WHERE name = ?", (name,)).fetchone()
    if row is None:
        abort(404)
    return render_template("firm.html", firm=firm_to_dict(row))


@app.route("/health")
def health():
    db = get_db()
    total = db.execute("SELECT COUNT(*) FROM firms").fetchone()[0]
    return {"ok": True, "firms": total}


@app.errorhandler(404)
def not_found(e):
    return render_template("404.html"), 404


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
