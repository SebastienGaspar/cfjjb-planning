#!/usr/bin/env python3
"""Quick inspector for output/planning.json.

Usage:
    python3 check_planning.py "Bleue - Adulte - Homme - Medio"
    python3 check_planning.py "Blanche - Adulte - Homme - Leve"
    python3 check_planning.py                     # list all categories
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
    data = json.loads((Path("output") / "planning.json").read_text(encoding="utf-8"))

    if len(sys.argv) < 2:
        cats = sorted({s.get("category", "") for s in data if s.get("category")},
                      key=norm)
        print(f"{len(cats)} categories, {len(data)} slots total:")
        for c in cats:
            n = sum(1 for s in data if s.get("category") == c)
            print(f"  [{n:>3}]  {c}")
        return 0

    target = norm(sys.argv[1])
    rows = [s for s in data if norm(s.get("category", "")) == target]
    rows.sort(key=lambda s: (s.get("date") or "", s.get("time") or ""))
    print(f"{len(rows)} slot(s) for '{sys.argv[1]}':")
    for s in rows:
        print(f"  {s.get('date'):<10}  {s.get('time'):<5}  "
              f"{s.get('mat'):<12}  page={s.get('fight_index')}/"
              f"{s.get('fight_total')}  note={s.get('note') or ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
