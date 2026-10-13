#!/usr/bin/env python3
"""Turn a plain-English request into a [[watch]] entry using Claude.

    python add_watch.py "Heathrow to Lagos, leave any Thu or Fri in Dec, back after 7-10 nights, under £550"
    python add_watch.py --append "..."      # also appends it to watches.toml

Needs: pip install anthropic, and ANTHROPIC_API_KEY set.
The model only fills a fixed schema; the tracker's alert logic stays plain code.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MODEL = os.environ.get("CLAUDE_MODEL", "claude-haiku-5-5")

WATCH_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "description": "Short unique label, e.g. 'London to Lagos, December'"},
        "origin": {"type": "string", "description": "3-letter IATA airport or metro code (LON = all London airports)"},
        "destination": {"type": "string", "description": "3-letter IATA airport or metro code"},
        "depart_from": {"type": "string", "description": "Earliest departure date, YYYY-MM-DD"},
        "depart_to": {"type": "string", "description": "Latest departure date, YYYY-MM-DD"},
        "depart_days": {"type": "array", "items": {"type": "string", "enum": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]},
                        "description": "Only if the user restricted departure weekdays"},
        "nights": {"type": "array", "items": {"type": "integer"}, "minItems": 2, "maxItems": 2,
                   "description": "[min, max] nights away for a return trip; omit for one-way"},
        "max_price": {"type": "number", "description": "Target price in the market currency"},
        "max_stops": {"type": "integer", "enum": [0, 1, 2], "description": "Only if the user stated a stops limit"},
        "adults": {"type": "integer", "minimum": 1, "maximum": 9},
        "cabin_class": {"type": "string", "enum": ["economy", "premium_economy", "business", "first"]},
        "market": {"type": "string", "description": "2-letter country code that sets currency; GB for GBP"},
    },
    "required": ["name", "origin", "destination", "depart_from", "depart_to", "max_price"],
}


def to_toml(w: dict) -> str:
    lines = ["", "[[watch]]"]
    for k, v in w.items():
        lines.append(f"{k} = {json.dumps(v, ensure_ascii=False)}")  # JSON scalars/arrays are valid TOML
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("request", help="what to track, in plain English")
    ap.add_argument("--append", action="store_true", help="append to watches.toml")
    args = ap.parse_args()

    try:
        import anthropic
    except ImportError:
        sys.exit("pip install anthropic")

    today = dt.date.today()
    client = anthropic.Anthropic()
    msg = client.messages.create(
        model=MODEL,
        max_tokens=1024,
        system=(
            f"Today is {today:%A %d %B %Y}. Convert the user's flight-tracking request into one watch "
            "by calling create_watch. Resolve relative dates against today. Use metro codes when the user "
            "names a city with several airports (London=LON). Default market GB unless the currency implies "
            "otherwise. Only include optional fields the user actually specified. Keep date ranges as stated; "
            "do not widen them."
        ),
        tools=[{"name": "create_watch", "description": "Create a fare watch", "input_schema": WATCH_SCHEMA}],
        tool_choice={"type": "tool", "name": "create_watch"},
        messages=[{"role": "user", "content": args.request}],
    )
    watch = next(b.input for b in msg.content if b.type == "tool_use")

    # Validate before trusting it
    block = to_toml(watch)
    tomllib.loads(block)
    d0, d1 = dt.date.fromisoformat(watch["depart_from"]), dt.date.fromisoformat(watch["depart_to"])
    if d1 < d0 or d1 < today:
        sys.exit(f"Date range looks wrong ({d0} → {d1}); not saving.\n{block}")

    print(block)
    if args.append:
        path = ROOT / "watches.toml"
        existing = tomllib.loads(path.read_text())
        if any(x["name"] == watch["name"] for x in existing.get("watch", [])):
            sys.exit(f"A watch named {watch['name']!r} already exists; not appending.")
        with open(path, "a") as f:
            f.write(block)
        print(f"Appended to {path.name}. Run `python tracker.py --plan` to check the call count.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
