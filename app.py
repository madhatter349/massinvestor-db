#!/usr/bin/env python3
"""Flask dashboard for the Massinvestor public directory database."""
import json
import os
import re
import sqlite3

from flask import Flask, abort, g, render_template, request
from urllib.parse import urlencode

DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(__file__), "massinvestor.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS firms (
    name TEXT PRIMARY KEY,
    type_key TEXT,
    website TEXT,
    offices TEXT,
    stages TEXT,
    industries TEXT,
    description TEXT,
    team_json TEXT,
    funding_json TEXT,
    portfolio_json TEXT,
    news_json TEXT,
    crawled_at TEXT
);
"""


def init_db():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()


init_db()

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
    top_states = sorted(states.items(), key=lambda x: -x[1])[:8]
    max_state = top_states[0][1] if top_states else 1

    industries = {}
    for r in db.execute("SELECT industries FROM firms"):
        for i in parse_json(r["industries"]):
            industries[i] = industries.get(i, 0) + 1
    top_industries = sorted(industries.items(), key=lambda x: -x[1])[:8]

    team_members = 0
    for r in db.execute("SELECT team_json FROM firms"):
        team_members += len(parse_json(r["team_json"]))
    portfolio_companies = 0
    for r in db.execute("SELECT portfolio_json FROM firms"):
        portfolio_companies += len(parse_json(r["portfolio_json"]))
    funding_events = 0
    for r in db.execute("SELECT funding_json FROM firms"):
        funding_events += len(parse_json(r["funding_json"]))

    recent = db.execute(
        "SELECT * FROM firms ORDER BY crawled_at DESC LIMIT 8"
    ).fetchall()
    return render_template(
        "index.html",
        total=total,
        team_members=team_members,
        portfolio_companies=portfolio_companies,
        funding_events=funding_events,
        types=[{"key": r["type_key"], "label": TYPE_LABELS.get(r["type_key"], r["type_key"] or "N/A"), "n": r["n"]} for r in types],
        top_states=[{"abbr": s, "n": n} for s, n in top_states],
        max_state=max_state,
        top_industries=[{"name": i, "n": n} for i, n in top_industries],
        recent=[firm_to_dict(r) for r in recent],
    )


SORT_OPTIONS = {
    "name": "Firm name (A–Z)",
    "name_desc": "Firm name (Z–A)",
    "team": "Team size (largest)",
    "portfolio": "Portfolio count (largest)",
    "funding": "Funding events (most)",
}


def qs_link(base, **changes):
    """Build a querystring keeping existing params except those changed."""
    args = {k: v for k, v in request.args.items() if v}
    for k, v in changes.items():
        if v:
            args[k] = v
        else:
            args.pop(k, None)
    return f"{base}?{urlencode(args)}" if args else base


def parse_qset(value):
    if not value:
        return []
    if isinstance(value, list):
        return [v for v in value if v]
    return [v for v in value.split(",") if v]


def is_list_facet(field, value):
    """Return SQL fragment that matches a value inside the stored JSON list."""
    return f"( ',' || substr({field}, 2, length({field}) - 2) || ',' LIKE '%,' || ? || ',%' )"


@app.route("/browse")
def browse():
    db = get_db()
    q = (request.args.get("q") or "").strip()
    page = max(1, int(request.args.get("page", 1) or 1))
    per_page = request.args.get("per")
    per_page = int(per_page) if per_page in ("50", "100", "250") else 50
    sort = request.args.get("sort", "name")
    letter = (request.args.get("letter") or "").strip().upper()
    types = parse_qset(request.args.getlist("type"))
    states = parse_qset(request.args.getlist("state"))
    industries = parse_qset(request.args.getlist("industry"))
    stages = parse_qset(request.args.getlist("stage"))

    where, params = [], []
    if q:
        where.append("(name LIKE ? OR description LIKE ? OR website LIKE ? OR offices LIKE ?)")
        like = f"%{q}%"
        params += [like, like, like, like]
    if letter:
        if letter == "#":
            where.append("(name GLOB '[0-9]*' OR name GLOB '##*')")
        else:
            where.append("name LIKE ?")
            params.append(f"{letter}%")
    for t in types:
        where.append(is_list_facet("type_key", t))
        params.append(t)
    for st in states:
        where.append(is_list_facet("offices", st))
        params.append(st)
    for ind in industries:
        where.append(is_list_facet("industries", ind))
        params.append(ind)
    for sg in stages:
        where.append(is_list_facet("stages", sg))
        params.append(sg)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    order_map = {
        "name": "name ASC",
        "name_desc": "name DESC",
        "team": "(CASE WHEN team_json='[]' THEN 0 ELSE 1 END) DESC, LENGTH(team_json) DESC, name ASC",
        "portfolio": "LENGTH(portfolio_json) DESC, name ASC",
        "funding": "LENGTH(funding_json) DESC, name ASC",
    }
    order_sql = order_map.get(sort, "name ASC")

    total = db.execute(f"SELECT COUNT(*) FROM firms {where_sql}", params).fetchone()[0]
    rows = db.execute(
        f"SELECT * FROM firms {where_sql} ORDER BY {order_sql} LIMIT ? OFFSET ?",
        params + [per_page, (page - 1) * per_page],
    ).fetchall()
    pages = max(1, (total + per_page - 1) // per_page)
    if page > pages:
        page = pages

    # facet option lists (all available values, not just filtered)
    facet_types = [{"key": r["type_key"], "label": TYPE_LABELS.get(r["type_key"], r["type_key"] or "N/A"), "n": r["n"]}
                   for r in db.execute(
                       "SELECT type_key, COUNT(*) n FROM firms WHERE type_key != '' GROUP BY type_key ORDER BY n DESC").fetchall()]
    state_rows = db.execute("SELECT offices FROM firms").fetchall()
    state_counts = {}
    for r in state_rows:
        for off in parse_json(r["offices"]):
            m = re.search(r"\b([A-Z]{2})\b(?:$|\s)", off)
            if m:
                state_counts[m.group(1)] = state_counts.get(m.group(1), 0) + 1
    facet_states = sorted(({"key": s, "n": n} for s, n in state_counts.items()),
                          key=lambda x: -x["n"])
    industry_counts = {}
    for r in db.execute("SELECT industries FROM firms").fetchall():
        for i in parse_json(r["industries"]):
            industry_counts[i] = industry_counts.get(i, 0) + 1
    facet_industries = sorted(({"key": i, "n": n} for i, n in industry_counts.items()),
                              key=lambda x: -x["n"])
    stage_counts = {}
    for r in db.execute("SELECT stages FROM firms").fetchall():
        for s in parse_json(r["stages"]):
            stage_counts[s] = stage_counts.get(s, 0) + 1
    facet_stages = sorted(({"key": s, "n": n} for s, n in stage_counts.items()),
                          key=lambda x: -x["n"])

    return render_template(
        "browse.html",
        firms=[firm_to_dict(r) for r in rows],
        q=q, page=page, pages=pages, total=total,
        per_page=per_page, sort=sort,
        sort_options=SORT_OPTIONS,
        letter=letter, types=types, states=states, industries=industries, stages=stages,
        facet_types=facet_types, facet_states=facet_states,
        facet_industries=facet_industries, facet_stages=facet_stages,
        active_facets=[
            *[{"kind": "type", "label": TYPE_LABELS.get(t, t or "N/A"), "value": t} for t in types],
            *[{"kind": "state", "label": st, "value": st} for st in states],
            *[{"kind": "industry", "label": i, "value": i} for i in industries],
            *[{"kind": "stage", "label": s, "value": s} for s in stages],
        ],
    )


@app.route("/firm/<path:name>")
def firm(name):
    db = get_db()
    row = db.execute("SELECT * FROM firms WHERE name = ?", (name,)).fetchone()
    if row is None:
        abort(404)
    firm = firm_to_dict(row)

    # prev / next in the A-Z order
    prev = db.execute(
        "SELECT name FROM firms WHERE name < ? ORDER BY name DESC LIMIT 1",
        (name,)).fetchone()
    nxt = db.execute(
        "SELECT name FROM firms WHERE name > ? ORDER BY name ASC LIMIT 1",
        (name,)).fetchone()

    # related: same top industry, same type, or same state (max 6)
    inds = firm["industries"]
    rel_sql = (
        "SELECT DISTINCT * FROM firms "
        "WHERE name != ? AND ("
        "   (industries LIKE ? OR industries LIKE ? OR industries LIKE ?) "
        "   OR (type_key = ? AND type_key != '') "
        "   OR offices LIKE ?"
        ") ORDER BY name LIMIT 6"
    )
    rel_params = [name]
    if inds:
        rel_params += [f"%{inds[0]}%", f"%{inds[0]}%", f"%{inds[0]}%"]
    else:
        rel_params += ["%", "%", "%"]
    rel_params += [firm["type_key"]]
    # offices state
    state_code = ""
    for off in firm["offices"]:
        m = re.search(r"\b([A-Z]{2})\b(?:$|\s)", off)
        if m:
            state_code = m.group(1)
            break
    rel_params.append(f"%{state_code}%")
    related = []
    try:
        for r in db.execute(rel_sql, rel_params).fetchall():
            rel_f = firm_to_dict(r)
            rel_f["industry_0"] = (rel_f["industries"][0] if rel_f["industries"] else "")
            related.append(rel_f)
    except Exception:
        related = []

    return render_template(
        "firm.html",
        firm=firm,
        prev_name=prev["name"] if prev else None,
        next_name=nxt["name"] if nxt else None,
        related=related,
    )


@app.route("/health")
def health():
    db = get_db()
    total = db.execute("SELECT COUNT(*) FROM firms").fetchone()[0]
    return {"ok": True, "firms": total}


@app.errorhandler(404)
def not_found(e):
    return render_template("404.html"), 404


app.jinja_env.globals["qs_link"] = qs_link


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
