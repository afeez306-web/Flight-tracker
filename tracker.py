#!/usr/bin/env python3
"""Flight fare tracker.

Polls the Ignav fare API for every route/date combination in watches.toml,
keeps price history in SQLite, and sends a Telegram alert when a fare is
under your target price or drops sharply below its recent low.

Standard library only (Python 3.11+).
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import os
import sqlite3
import sys
import time
import tomllib
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BASE_URL = os.environ.get("IGNAV_BASE_URL", "https://ignav.com/api").rstrip("/")
DB_PATH = Path(os.environ.get("FARE_DB", ROOT / "fares.db"))
COST_PER_CALL_USD = 0.002  # Ignav: $2 per 1,000 successful requests after the free 1,000

# Optional Ignav filters copied straight from a watch into the request
PASSTHROUGH = ("children", "min_checked_bags", "min_carry_on_bags", "airlines_include",
               "airlines_exclude", "allow_self_transfer", "departure_time_range", "return_time_range")

WEEKDAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}

DEFAULTS = {
    "market": "GB",
    "adults": 1,
    "cabin_class": "economy",
    "max_stops": None,
    "drop_pct": 15,
    "history_days": 30,
    "max_calls_per_run": 150,
    "runs_per_day": 2,
    "max_rows_in_alert": 5,
}


# ---------------------------------------------------------------- config ---

def load_config(path: Path) -> tuple[dict, list[dict]]:
    with open(path, "rb") as f:
        cfg = tomllib.load(f)
    settings = {**DEFAULTS, **cfg.get("settings", {})}
    watches = cfg.get("watch", [])
    if not watches:
        sys.exit(f"No [[watch]] entries found in {path}")
    for w in watches:
        missing = [k for k in ("name", "origin", "destination", "depart_from", "depart_to", "max_price") if k not in w]
        if missing:
            sys.exit(f"Watch {w.get('name', '?')!r} is missing: {', '.join(missing)}")
        for k in ("market", "adults", "cabin_class", "max_stops", "drop_pct"):
            w.setdefault(k, settings[k])
    names = [w["name"] for w in watches]
    if len(names) != len(set(names)):
        sys.exit("Each watch needs a unique name")
    return settings, watches


def combos_for(w: dict, today: dt.date) -> list[tuple[dt.date, dt.date | None]]:
    start = max(dt.date.fromisoformat(str(w["depart_from"])), today)
    end = dt.date.fromisoformat(str(w["depart_to"]))
    days = {WEEKDAYS[d.lower()[:3]] for d in w.get("depart_days", [])}
    departs = []
    d = start
    while d <= end:
        if not days or d.weekday() in days:
            departs.append(d)
        d += dt.timedelta(days=1)

    nights = w.get("nights")  # [min, max] -> round trip; absent -> one way
    if nights is None:
        return [(d, None) for d in departs]
    lo, hi = (nights, nights) if isinstance(nights, int) else (nights[0], nights[1])
    return [(d, d + dt.timedelta(days=n)) for d in departs for n in range(lo, hi + 1)]


# ------------------------------------------------------------------- api ---

def api_post(path: str, body: dict, key: str, retries: int = 3) -> dict:
    data = json.dumps(body).encode()
    for attempt in range(retries + 1):
        req = urllib.request.Request(
            f"{BASE_URL}{path}",
            data=data,
            headers={"X-Api-Key": key, "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < retries:
                time.sleep(2 ** attempt * 3)
                continue
            detail = e.read().decode(errors="replace")[:300]
            raise RuntimeError(f"HTTP {e.code} on {path}: {detail}") from None
        except urllib.error.URLError as e:
            if attempt < retries:
                time.sleep(2 ** attempt * 3)
                continue
            raise RuntimeError(f"Network error on {path}: {e.reason}") from None
    raise RuntimeError("unreachable")


def search(w: dict, depart: dt.date, ret: dt.date | None, key: str) -> dict:
    body = {
        "origin": w["origin"],
        "destination": w["destination"],
        "departure_date": depart.isoformat(),
        "adults": w["adults"],
        "cabin_class": w["cabin_class"],
        "market": w["market"],
    }
    if w["max_stops"] is not None:
        body["max_stops"] = w["max_stops"]
    for opt in PASSTHROUGH:
        if opt in w:
            body[opt] = w[opt]
    if ret:
        body["return_date"] = ret.isoformat()
        return api_post("/fares/round-trip", body, key)
    return api_post("/fares/one-way", body, key)


def booking_link(ignav_id: str, key: str) -> str | None:
    try:
        resp = api_post("/fares/booking-links", {"ignav_id": ignav_id}, key)
    except RuntimeError as e:
        print(f"  booking link lookup failed: {e}")
        return None
    links = [l for opt in resp.get("booking_options", []) for l in opt.get("links", []) if l.get("url")]
    if not links:
        return None
    priced = [l for l in links if (l.get("price") or {}).get("amount")]
    best = min(priced, key=lambda l: l["price"]["amount"]) if priced else links[0]
    url = best["url"]
    return url if url.startswith("http") else f"https://{url}"


def cheapest(resp: dict) -> dict | None:
    its = [i for i in resp.get("itineraries", []) if (i.get("price") or {}).get("amount", 0) > 0]
    return min(its, key=lambda i: i["price"]["amount"]) if its else None


def describe_leg(leg: dict | None) -> str:
    if not leg:
        return ""
    segs = leg.get("segments", [])
    stops = max(len(segs) - 1, 0)
    via = ", ".join(s["arrival_airport"] for s in segs[:-1])
    h, m = divmod(leg.get("duration_minutes", 0), 60)
    stop_txt = "direct" if stops == 0 else f"{stops} stop{'s' if stops > 1 else ''} via {via}"
    dep = segs[0]["departure_time_local"][11:16] if segs else ""
    return f"{leg.get('carrier', '?')} {dep}, {stop_txt}, {h}h{m:02d}"


def describe(it: dict) -> str:
    parts = [describe_leg(it.get("outbound"))]
    if it.get("inbound"):
        parts.append("back: " + describe_leg(it["inbound"]))
    if it.get("requires_self_transfer"):
        parts.append("self-transfer")
    return " | ".join(p for p in parts if p)


# --------------------------------------------------------------- storage ---

def db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS prices (
            watch TEXT, depart TEXT, ret TEXT, amount REAL, currency TEXT,
            observed_at TEXT, ignav_id TEXT, summary TEXT);
        CREATE INDEX IF NOT EXISTS idx_prices ON prices (watch, depart, ret, observed_at);
        CREATE TABLE IF NOT EXISTS alerts (
            watch TEXT, depart TEXT, ret TEXT, amount REAL, sent_at TEXT);
        """
    )
    return conn


def evaluate(conn, w, depart, ret, amount, history_days) -> list[str]:
    """Return the reasons to alert (empty list = no alert)."""
    key = (w["name"], depart, ret or "")
    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=history_days)).isoformat()
    prev_min = conn.execute(
        "SELECT MIN(amount) FROM prices WHERE watch=? AND depart=? AND ret=? AND observed_at>=?",
        (*key, since),
    ).fetchone()[0]

    reasons = []
    if amount <= w["max_price"]:
        reasons.append(f"under your {w['max_price']:g} target")
    if prev_min and amount <= prev_min * (1 - w["drop_pct"] / 100):
        reasons.append(f"{round((1 - amount / prev_min) * 100)}% below {history_days}-day low of {prev_min:g}")
    if not reasons:
        return []

    last_alert = conn.execute(
        "SELECT amount FROM alerts WHERE watch=? AND depart=? AND ret=? ORDER BY sent_at DESC LIMIT 1", key
    ).fetchone()
    if last_alert and amount >= last_alert[0]:
        return []  # already told you about this price or a lower one
    return reasons


# ---------------------------------------------------------------- alerts ---

def send_telegram(text: str) -> None:
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not (token and chat):
        print("\n--- alert (Telegram not configured, printing instead) ---\n" + text + "\n")
        return
    body = json.dumps({"chat_id": chat, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=body, headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        if json.load(r).get("ok") is not True:
            raise RuntimeError("Telegram rejected the message")


def format_alert(w: dict, hits: list[dict], link: str | None, max_rows: int) -> str:
    e = html.escape
    trip = "return" if hits[0]["ret"] else "one-way"
    lines = [f"✈️ <b>{e(w['name'])}</b> — {e(w['origin'])} → {e(w['destination'])} ({trip})"]
    for h in hits[:max_rows]:
        dates = h["depart"] + (f" → {h['ret']}" if h["ret"] else "")
        lines.append(f"\n<b>{h['amount']:g} {e(h['currency'])}</b>  {e(dates)}")
        lines.append(f"{e(h['summary'])}")
        lines.append(f"<i>{e('; '.join(h['reasons']))}</i>")
    if len(hits) > max_rows:
        lines.append(f"\n…and {len(hits) - max_rows} more matching dates")
    if link:
        lines.append(f'\n<a href="{e(link)}">Book the cheapest</a>')
    lines.append("\nPrices are search quotes — confirm on the booking page.")
    return "\n".join(lines)


# ------------------------------------------------------------------ main ---

def plan(settings: dict, watches: list[dict], today: dt.date) -> int:
    total = 0
    for w in watches:
        n = len(combos_for(w, today))
        total += n
        print(f"{w['name']}: {n} searches per run")
    monthly = total * settings["runs_per_day"] * 30
    print(f"\nTotal: {total} calls/run, ~{monthly:,} calls/month at {settings['runs_per_day']} runs/day "
          f"≈ ${monthly * COST_PER_CALL_USD:.2f}/month (plus booking-link calls on alerts)")
    if total > settings["max_calls_per_run"]:
        print(f"⚠️  Over max_calls_per_run ({settings['max_calls_per_run']}). Narrow dates/nights or raise the cap.")
    return total


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--config", default=ROOT / "watches.toml", type=Path)
    ap.add_argument("--plan", action="store_true", help="show call counts and cost estimate, then exit")
    args = ap.parse_args()

    settings, watches = load_config(args.config)
    today = dt.date.today()

    total = plan(settings, watches, today)
    if args.plan:
        return 0
    if total > settings["max_calls_per_run"]:
        return 2

    key = os.environ.get("IGNAV_API_KEY")
    if not key:
        sys.exit("Set IGNAV_API_KEY")

    conn = db_connect()
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    failures = calls = 0

    for w in watches:
        hits = []
        print(f"\n== {w['name']}")
        for depart, ret in combos_for(w, today):
            calls += 1
            try:
                it = cheapest(search(w, depart, ret, key))
            except RuntimeError as e:
                failures += 1
                print(f"  {depart} {ret or ''}: {e}")
                continue
            if not it:
                print(f"  {depart} {ret or ''}: no fares")
                continue
            d, r = depart.isoformat(), ret.isoformat() if ret else ""
            amount, currency = float(it["price"]["amount"]), it["price"]["currency"]
            summary = describe(it)
            reasons = evaluate(conn, w, d, r, amount, settings["history_days"])
            conn.execute(
                "INSERT INTO prices VALUES (?,?,?,?,?,?,?,?)",
                (w["name"], d, r, amount, currency, now, it.get("ignav_id"), summary),
            )
            print(f"  {d} {r}: {amount:g} {currency}{'  <-- ALERT' if reasons else ''}")
            if reasons:
                hits.append({"depart": d, "ret": r, "amount": amount, "currency": currency,
                             "summary": summary, "reasons": reasons, "ignav_id": it.get("ignav_id")})
        conn.commit()

        if hits:
            hits.sort(key=lambda h: h["amount"])
            link = booking_link(hits[0]["ignav_id"], key) if hits[0]["ignav_id"] else None
            send_telegram(format_alert(w, hits, link, settings["max_rows_in_alert"]))
            conn.executemany(
                "INSERT INTO alerts VALUES (?,?,?,?,?)",
                [(w["name"], h["depart"], h["ret"], h["amount"], now) for h in hits],
            )
            conn.commit()

    conn.close()
    print(f"\nDone: {calls} searches, {failures} failed.")
    if calls and failures == calls:
        print("Every search failed — check the API key and Ignav status.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
