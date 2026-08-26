#!/usr/bin/env python3
"""Scraper for the public Massinvestor investment-firm directory.

Index:   https://massinvestordatabase.com/investmentfirmlist.php?range=<abc..vwxyz>
Detail:  https://massinvestordatabase.com/publicfirm.php?name=<urlencoded name>

Firm data (names, types, offices, stages, industries, descriptions, teams,
funding events, portfolio companies, news) is publicly served. Emails and full
partner profiles are behind a paid login and are NOT scraped.

Public pages have no robots.txt (404). ToS (terms.php) prohibits copying and
redistribution, so this tool is for personal/private research use only.
"""

import argparse
import html
import json
import os
import random
import re
import sqlite3
import sys
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

BASE = "https://massinvestordatabase.com"
LIST_SLUGS = ["abc", "def", "ghi", "jkl", "mno", "pqr", "stu", "vwxyz"]
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

FIRM_LINK_RE = re.compile(
    r'<a[^>]*href="[^"]*?"[^>]*id="firmtitle">([^<]+)</a>'
)

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
CREATE TABLE IF NOT EXISTS raw (
    name TEXT PRIMARY KEY,
    html TEXT,
    http_status INTEGER
);
"""


def clean(text):
    return re.sub(r"\s+", " ", text).strip()


def strip_tags(seg):
    seg = re.sub(r"<br\s*/?>", "\n", seg)
    seg = re.sub(r"</(li|p|tr)>", "\n", seg)
    seg = re.sub(r"<[^>]+>", " ", seg)
    seg = urllib.parse.unquote(seg)
    seg = re.sub(r"&nbsp;", " ", seg)
    seg = html.unescape(seg)
    return seg


def extract_section(htm, marker, min_height=False):
    idx = htm.find(marker)
    if idx == -1:
        return []
    seg = htm[idx:idx + 30000]
    if min_height:
        m = re.search(r'<div id="descriptionDiv"[^>]*>(.*?)</div>', seg, re.S)
        if m:
            seg = m.group(1)
            return [clean(l) for l in strip_tags(seg).split("\n") if clean(l)]
    end = seg.find("</table>")
    seg = seg[:end] if end != -1 else seg[:20000]
    lines = [clean(l) for l in strip_tags(seg).split("\n") if clean(l)]
    drop = re.compile(
        r"(?i)^(get the complete|.*subscription|.*free demo|click here|"
        r".*national database.*|.*silicon valley database.*|stages:|"
        r"industries:|description:|office locations:|recent funding events.*|"
        r"portfolio companies include:|recent news:|investment team:|"
        r"investment firm key|vc.*venture capital|pe.*private equity|"
        r"^a\s*=.*angel|^i\s*=.*incubator|^mb\s*=.*merchant|^vd\s*=.*debt|"
        r"^fi\s*=.*family|^fof\s*=.*fund of funds|^ed\s*=.*economic|"
        r"^tt\s*=.*technology|^cvc\s*=.*corporate|^sec\s*=.*secondary|"
        r"^hf\s*=.*hedge)"
    )
    return [l for l in lines if not drop.match(l)]


def extract_team(htm):
    idx = htm.find("id=\"investmentDiv\"")
    if idx == -1:
        return []
    seg = htm[idx:idx + 20000]
    rows = re.findall(
        r"<td[^>]*>\s*([^<]{1,100}?)\s*</td>\s*<td[^>]*></td>"
        r"\s*<td[^>]*></td>\s*<td[^>]*></td>\s*<td[^>]*>\s*([^<]{1,100}?)\s*</td>",
        seg,
    )
    team = []
    for name, title in rows:
        n, t = clean(name), clean(title)
        if not n or n in ("Name",) or t in ("Title",):
            continue
        if re.search(r"(?i)get the complete|subscription|email|linkedin", n + " " + t):
            continue
        team.append({"name": n, "title": t})
    return team


def extract_funding(htm):
    idx = htm.find("Recent Funding Events")
    if idx == -1:
        return []
    seg = htm[idx:idx + 30000]
    end = seg.find("Portfolio companies include:")
    seg = seg[:end] if end != -1 else seg[:20000]
    rows = re.findall(
        r"<tr>\s*<td[^>]*>\s*<br[^>]*>\s*&nbsp;&nbsp;(.*?)</td>"
        r"\s*<td[^>]*>\s*<br[^>]*>\s*&nbsp;&nbsp;(.*?)</td>"
        r"\s*<td[^>]*>\s*<br[^>]*>\s*&nbsp;&nbsp;(.*?)</td>"
        r"\s*<td[^>]*>\s*<br[^>]*>\s*&nbsp;&nbsp;(.*?)</td>"
        r"\s*<td[^>]*>\s*<br[^>]*>\s*&nbsp;&nbsp;(.*?)</td>",
        seg,
        re.S,
    )
    out = []
    for r in rows:
        cells = [clean(html.unescape(re.sub(r"<[^>]+>", "", x))) for x in r]
        date, name, state, amount, stage = cells
        if not name:
            continue
        out.append({"date": date, "name": name, "state": state,
                    "amount": amount, "stage": stage})
    return out


def extract_portfolio(htm):
    idx = htm.find("Portfolio companies include:")
    if idx == -1:
        return []
    seg = htm[idx:idx + 40000]
    end = seg.find("Recent News:")
    seg = seg[:end] if end != -1 else seg[:30000]
    rows = re.findall(
        r'&nbsp;&nbsp;<a[^>]*href="[^"]*"[^>]*>(.*?)</a>'
        r'</span>\s*(?:<br/>\s*<span[^>]*>&nbsp;&nbsp;&nbsp;&nbsp;'
        r'<a[^>]*href="(https?://[^"]+)"[^>]*>web link</a>\s*</span>)?',
        seg,
        re.S,
    )
    out = []
    for name, website in rows:
        n = clean(html.unescape(re.sub(r"<[^>]+>", "", name)))
        if n:
            out.append({"name": n, "website": website or ""})
    return out


def extract_news(htm):
    idx = htm.find("id=\"newsDiv\"")
    if idx == -1:
        return []
    seg = htm[idx:idx + 30000]
    items = re.findall(r"<li>\s*<a[^>]*href=\"([^\"]+)\"[^>]*>(.*?)</a>\s*</li>", seg, re.S)
    out = []
    for url, i in items:
        text = clean(html.unescape(re.sub(r"<[^>]+>", "", i)))
        if text:
            out.append({"title": text, "url": url})
    return out


def is_valid_detail(htm):
    return htm.count("id=\"sectionheader\"") >= 2 and "investmentDiv" in htm


def parse_profile(htm, url):
    title = re.search(r"<title>(.*?)</title>", htm, re.S)
    title_text = clean(html.unescape(title.group(1))) if title else ""
    firm_name = re.sub(r"\s*-\s*Massinvestor.*$", "", title_text).strip()

    type_m = re.search(
        r'<span style="font-family:Rockwell,Arial,serif;color:#81c144;">'
        r'([A-Za-z/]{1,16})</span>', htm)
    type_key = type_m.group(1).upper() if type_m else ""

    web_m = re.search(
        r'<a href="(https?://[^"]+)" target="_blank">\1</a>', htm)
    website = web_m.group(1) if web_m else ""

    return {
        "url": url,
        "name": firm_name,
        "type_key": type_key,
        "website": website,
        "offices": extract_section(htm, "Office Locations:"),
        "stages": extract_section(htm, "Stages:"),
        "industries": extract_section(htm, "Industries:"),
        "description": " ".join(extract_section(htm, "Description:", min_height=True)),
        "team": extract_team(htm),
        "funding": extract_funding(htm),
        "portfolio": extract_portfolio(htm),
        "news": extract_news(htm),
    }


def fetch(url, delay, session):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml",
        "Cookie": session,
    })
    last = None
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = resp.read().decode("utf-8", errors="replace")
            time.sleep(delay + random.uniform(0, 0.3))
            return data, resp.status
        except urllib.error.HTTPError as e:
            last = e
            if e.code in (403, 429):
                time.sleep(delay * 5 * (attempt + 1))
                continue
            break
        except Exception as e:
            last = e
            time.sleep(delay * 3 * (attempt + 1))
    print(f"FAILED {url}: {last}", file=sys.stderr)
    return None, None


def get_firm_names(html_page):
    return sorted({clean(n) for n in FIRM_LINK_RE.findall(html_page)})


def scrape_list(slug, conn, args, session):
    url = f"{BASE}/investmentfirmlist.php?range={slug}"
    htm, status = fetch(url, args.delay, session)
    if htm is None:
        print(f"list {slug}: failed", file=sys.stderr)
        return []
    names = get_firm_names(htm)
    conn.execute("INSERT OR IGNORE INTO raw(name, html, http_status) VALUES (?,?,?)",
                 (f"__list_{slug}", htm, status))
    conn.commit()
    return names


def crawl():
    ap = argparse.ArgumentParser(description="Scrape Massinvestor public directory")
    ap.add_argument("--db", default="massinvestor.db")
    ap.add_argument("--delay", type=float, default=0.6)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--limit", type=int, default=0, help="0 = all firms")
    ap.add_argument("--fresh", action="store_true", help="reset DB and rescrape")
    args = ap.parse_args()

    if args.fresh and os.path.exists(args.db):
        os.remove(args.db)
    conn = sqlite3.connect(args.db, check_same_thread=False)
    conn.executescript(SCHEMA)
    conn.commit()
    db_lock = threading.Lock()

    session = ""

    all_names = set()
    for slug in LIST_SLUGS:
        names = scrape_list(slug, conn, args, session)
        all_names.update(names)
        print(f"list {slug}: {len(names)} firms (running unique {len(all_names)})")

    done = {r[0] for r in conn.execute("SELECT name FROM firms")}
    pending = sorted(all_names - done)
    if args.limit:
        pending = pending[:args.limit]
    print(f"unique firms: {len(all_names)}  already scraped: {len(done)}  "
          f"to fetch: {len(pending)}")

    def work(name):
        url = f"{BASE}/publicfirm.php?name={urllib.parse.quote_plus(name)}"
        htm, status = fetch(url, args.delay, session)
        if htm is None:
            return name, None
        valid = is_valid_detail(htm)
        if not valid:
            with db_lock:
                conn.execute("INSERT OR REPLACE INTO raw(name, html, http_status) "
                             "VALUES (?,?,?)", (name, htm, status))
                conn.commit()
            return name, {"error": f"invalid page (status {status})"}
        profile = parse_profile(htm, url)
        profile["name"] = profile["name"] or name
        with db_lock:
            conn.execute(
                """INSERT OR REPLACE INTO firms
                   (name,type_key,website,offices,stages,industries,description,
                    team_json,funding_json,portfolio_json,news_json,crawled_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (profile["name"], profile["type_key"], profile["website"],
                 json.dumps(profile["offices"]), json.dumps(profile["stages"]),
                 json.dumps(profile["industries"]), profile["description"],
                 json.dumps(profile["team"]), json.dumps(profile["funding"]),
                 json.dumps(profile["portfolio"]), json.dumps(profile["news"]),
                 time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())))
            conn.execute("INSERT OR REPLACE INTO raw(name, html, http_status) "
                         "VALUES (?,?,?)", (name, htm, status))
            conn.commit()
        return name, profile

    done_count = len(done)
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(work, n): n for n in pending}
        for i, fut in enumerate(as_completed(futs), 1):
            name, result = fut.result()
            if result is None:
                print(f"[{i}/{len(pending)}] FAIL {name}", file=sys.stderr)
            elif "error" in result:
                print(f"[{i}/{len(pending)}] NOTFOUND {name}")
            else:
                done_count += 1
            if i % 50 == 0:
                print(f"[{i}/{len(pending)}] completed ({done_count} total)")

    rows = conn.execute("SELECT COUNT(*) FROM firms").fetchone()[0]
    print(f"DONE. {rows} firms in {args.db}")


if __name__ == "__main__":
    crawl()
