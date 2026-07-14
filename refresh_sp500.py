"""
Regenerate data/sp500.json (the momentum-screen universe) from the same source it
has always come from: the `datasets/s-and-p-500-companies` constituents.csv, which
mirrors the Wikipedia S&P 500 table.

Writes BOTH the flat `symbols` list AND a parallel `sectors` map
(symbol -> {sector, sub_industry}) in one pull, so membership and GICS
classification can never drift apart. The sectors map feeds the momentum screen's
EXCLUDED_SECTORS filter (config.EXCLUDED_SECTORS).

Only stdlib csv + requests are used (lxml/bs4 are NOT installed, so pandas.read_html
is unavailable — and unnecessary here).

Run:
  python3 refresh_sp500.py            # rewrite data/sp500.json, print a summary
  python3 refresh_sp500.py --dry-run  # print the summary + symbol diff, write nothing
"""

import argparse
import csv
import io
import json
import os
import sys
from datetime import date

import requests

CSV_URL = ("https://raw.githubusercontent.com/datasets/"
           "s-and-p-500-companies/main/data/constituents.csv")
SOURCE_LABEL = "github.com/datasets/s-and-p-500-companies (constituents.csv)"
OUT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "sp500.json")


def _fetch_rows() -> list[dict]:
    resp = requests.get(CSV_URL, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
    resp.raise_for_status()
    rows = list(csv.DictReader(io.StringIO(resp.text)))
    if not rows:
        raise RuntimeError("constituents.csv came back empty")
    required = {"Symbol", "GICS Sector", "GICS Sub-Industry"}
    missing = required - set(rows[0].keys())
    if missing:
        raise RuntimeError(f"constituents.csv missing columns: {sorted(missing)}")
    return rows


def _build(rows: list[dict]) -> dict:
    """Build the sp500.json document. Symbols are sorted so the file is stable and
    diffable across refreshes; the sectors map is keyed by the same symbol form."""
    by_symbol: dict[str, dict] = {}
    for r in rows:
        sym = (r.get("Symbol") or "").strip()
        if not sym:
            continue
        by_symbol[sym] = {
            "sector":       (r.get("GICS Sector") or "").strip(),
            "sub_industry": (r.get("GICS Sub-Industry") or "").strip(),
        }
    symbols = sorted(by_symbol)
    sectors = {s: by_symbol[s] for s in symbols}
    return {
        "symbols":        symbols,
        "sectors":        sectors,
        "count":          len(symbols),
        "source":         SOURCE_LABEL,
        "sectors_source": SOURCE_LABEL,
        "as_of":          date.today().isoformat(),
        "note": ("S&P 500 membership + GICS sector/sub-industry snapshot; refresh "
                 "quarterly with refresh_sp500.py. Universe + sector filter source "
                 "for momentum_screen.py."),
    }


def _existing_symbols() -> list[str]:
    try:
        with open(OUT_PATH) as f:
            return list(json.load(f).get("symbols", []))
    except (OSError, json.JSONDecodeError):
        return []


def _write(doc: dict) -> None:
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    tmp = f"{OUT_PATH}.tmp"
    with open(tmp, "w") as f:
        json.dump(doc, f, indent=2)
        f.write("\n")
    os.replace(tmp, OUT_PATH)


def main() -> int:
    parser = argparse.ArgumentParser(description="Regenerate data/sp500.json (symbols + GICS sectors)")
    parser.add_argument("--dry-run", action="store_true",
                        help="print summary + symbol diff, write nothing")
    args = parser.parse_args()

    rows = _fetch_rows()
    doc = _build(rows)

    old = set(_existing_symbols())
    new = set(doc["symbols"])
    added = sorted(new - old)
    removed = sorted(old - new)

    print(f"Fetched {len(rows)} constituents -> {doc['count']} symbols with sectors")
    if old:
        print(f"Symbol diff vs current file: +{len(added)} -{len(removed)}")
        if added:
            print(f"  added:   {added}")
        if removed:
            print(f"  removed: {removed}")
    else:
        print("No existing sp500.json to diff against.")

    if args.dry_run:
        print("[dry-run] not writing", OUT_PATH)
        return 0

    _write(doc)
    print("Wrote", OUT_PATH)
    return 0


if __name__ == "__main__":
    sys.exit(main())
