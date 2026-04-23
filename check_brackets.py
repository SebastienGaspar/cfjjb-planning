#!/usr/bin/env python3
"""Inspector for output/brackets_by_category.json.

Usage:
    python3 check_brackets.py                      # summary of every category
    python3 check_brackets.py "Jane DOE"           # find a fighter
    python3 check_brackets.py "Bleue - Adulte"     # find a category (substring)
"""

import json
import sys
import unicodedata
from pathlib import Path


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return " ".join(s.lower().split())


def main() -> int:
    data = json.loads((Path("output") / "brackets_by_category.json").read_text(encoding="utf-8"))

    if len(sys.argv) < 2:
        populated = sum(1 for b in data if b.get("status") == "populated")
        total_fighters = sum(len(b.get("fighters", [])) for b in data)
        print(f"{len(data)} category/ies ({populated} populated), "
              f"{total_fighters} fighter slot(s) total:")
        for b in sorted(data, key=lambda b: norm(b.get("raw", ""))):
            n = len(b.get("fighters", []))
            pt = b.get("page_total") or "?"
            ct = b.get("combatants_total") or "?"
            print(f"  [{n:>3} / {ct}]  pages={pt}  {b.get('raw')}")
        return 0

    target = norm(sys.argv[1])

    # Fighter name match first.
    fighter_hits: list[tuple[dict, dict]] = []
    for b in data:
        for f in b.get("fighters", []):
            if target in norm(f.get("name", "")):
                fighter_hits.append((b, f))
    if fighter_hits:
        print(f"{len(fighter_hits)} fighter(s) matching '{sys.argv[1]}':")
        for b, f in fighter_hits:
            print(f"  {f.get('name')}  team={f.get('team', '')}  "
                  f"seed={f.get('seed')}  page={f.get('page')}/{b.get('page_total')}  "
                  f"mat={f.get('mat')}  cat={b.get('raw')}")
        return 0

    # Otherwise: category substring match.
    cat_hits = [b for b in data if target in norm(b.get("raw", ""))]
    if not cat_hits:
        print(f"No match for '{sys.argv[1]}'.")
        return 1
    for b in cat_hits:
        print(f"Category: {b.get('raw')}")
        print(f"  page_total       = {b.get('page_total')}")
        print(f"  combatants_total = {b.get('combatants_total')}")
        print(f"  weight_limit     = {b.get('weight_limit')}")
        print(f"  status           = {b.get('status')}")
        print(f"  pages_detail:")
        for pd in b.get("pages_detail", []):
            print(f"    page {pd.get('page')}/{pd.get('page_total')}  "
                  f"mat={pd.get('mat')}  fighters={len(pd.get('fighters', []))}")
        print(f"  {len(b.get('fighters', []))} fighter(s):")
        for f in b.get("fighters", []):
            seed = f.get('seed')
            page = f.get('page')
            print(f"    seed={str(seed):<4}  page={str(page):<4}  "
                  f"mat={str(f.get('mat', '')):<14}  "
                  f"{f.get('name')}  ({f.get('team', '')})")
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
