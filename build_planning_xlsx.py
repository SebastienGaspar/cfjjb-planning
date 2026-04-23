#!/usr/bin/env python3
"""
Build an Excel planning for a given academy from the three DOM dumps
produced by extract_cfjjb.py:

  output/participants_by_team.json  — who is registered, per team
  output/planning.json              — each fight slot on each mat
  output/brackets_by_category.json  — pages per category with fighters & seeds

Join logic:
  athlete (from participants)
    -> bracket category
       -> the page (1..N) on which the athlete sits
          -> planning slot with matching (category, page, mat)
             -> date, time, mat

Output columns:
  Participant | Équipe | Catégorie | Date | Heure | Tapis | Tableaux | Note
  (Tableaux is "N/M" — the athlete's bracket page out of the total pages.)

One row per athlete (no more "same category × N slots" blow-up).
Output lands in output/planning_<academy>.xlsx.

Usage:
    pip install openpyxl
    python3 build_planning_xlsx.py --academy "Example Academy"
"""

from __future__ import annotations

import argparse
import json
import sys
import unicodedata
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter


# ---------- helpers ----------

def norm(s: str | None) -> str:
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return " ".join(s.lower().split())


def load_json(path: Path) -> object:
    if not path.exists():
        print(f"[!] missing: {path}", file=sys.stderr)
        return None
    return json.loads(path.read_text(encoding="utf-8"))


# ---------- shaping ----------

def collect_academy_athletes(participants: list[dict], academy: str) -> list[dict]:
    target = norm(academy)
    out: list[dict] = []
    for team_block in participants or []:
        team = team_block.get("team") or ""
        if target not in norm(team):
            continue
        for ath in team_block.get("athletes", []):
            out.append({
                "name": ath.get("name", ""),
                "team": team,
                "category": ath.get("category", ""),
            })
    return out


def index_brackets(brackets: list[dict]) -> dict[str, dict]:
    return {norm(b.get("raw") or b.get("category", "")): b for b in brackets or []}


def index_planning(planning: list[dict]) -> dict[tuple, list[dict]]:
    """key = (category_norm, page_or_None, mat_norm) -> slots.
       Also stores a fallback key (category_norm, None, None) for single-page cats."""
    idx: dict[tuple, list[dict]] = {}
    for f in planning or []:
        cat = norm(f.get("category", ""))
        if not cat:
            continue
        page = f.get("fight_index")  # scraper field = page number of the tableau
        mat  = norm(f.get("mat", ""))
        idx.setdefault((cat, page, mat), []).append(f)
        idx.setdefault((cat, None, None), []).append(f)  # broad fallback
    return idx


def locate_athlete_in_bracket(name: str, bracket: dict | None) -> tuple[int | None, int | None, str, dict | None]:
    """Return (seed, page, mat_from_bracket, bracket_entry_for_name)."""
    if not bracket:
        return (None, None, "", None)
    target = norm(name)
    for f in bracket.get("fighters", []) or []:
        if norm(f.get("name")) == target:
            return (f.get("seed"), f.get("page"), f.get("mat") or "", f)
    return (None, None, "", None)


# ---------- writer ----------

HEADERS = [
    "Participant", "Équipe", "Catégorie",
    "Date", "Heure", "Tapis", "Tableaux", "Note",
]

BELT_FILLS = {
    "blanche":  "FFFFFF",
    "bleue":    "DBEAFE",
    "violette": "E9D5FF",
    "marron":   "D7B899",
    "noire":    "374151",
}


def write_xlsx(rows: list[dict], out_path: Path, academy: str) -> None:
    wb = Workbook()
    ws = wb.active
    ws.title = "Planning"

    ws.append([f"Planning — {academy}"] + [""] * (len(HEADERS) - 1))
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=len(HEADERS))
    ws["A1"].font = Font(bold=True, size=14)
    ws["A1"].alignment = Alignment(horizontal="center")

    ws.append(HEADERS)
    header_fill = PatternFill("solid", fgColor="1E3A8A")
    header_font = Font(bold=True, color="FFFFFF")
    for col in range(1, len(HEADERS) + 1):
        c = ws.cell(row=2, column=col)
        c.fill = header_fill
        c.font = header_font
        c.alignment = Alignment(horizontal="center", vertical="center")

    for r in rows:
        page = r.get("page")
        total = r.get("page_total")
        if page is not None and total:
            tableaux = f"{page}/{total}"
        elif page is not None:
            tableaux = f"{page}"
        elif r.get("has_slot"):
            # The planning slot had no "N/M" pill — the bracket is a single
            # page (e.g. P.Seule or a small category). Render as 1/1.
            tableaux = "1/1"
        else:
            tableaux = ""
        ws.append([
            r["name"], r["team"], r["category"],
            r["date"], r["time"], r["mat"],
            tableaux,
            r["note"] or "",
        ])
        belt = norm(r.get("belt"))
        fill_hex = BELT_FILLS.get(belt)
        if fill_hex:
            fill = PatternFill("solid", fgColor=fill_hex)
            font_color = "FFFFFF" if belt == "noire" else "000000"
            for col in (1, 2, 3):
                cell = ws.cell(row=ws.max_row, column=col)
                cell.fill = fill
                cell.font = Font(color=font_color)

    widths = [28, 24, 38, 12, 8, 12, 10, 28]
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w

    ws.freeze_panes = "A3"
    ws.auto_filter.ref = f"A2:{get_column_letter(len(HEADERS))}{ws.max_row}"
    wb.save(out_path)


# ---------- main ----------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--academy", required=True, help="Academy to build the planning for (e.g. 'Example Academy').")
    ap.add_argument("--input-dir", default="output", help="Folder with the three JSON files.")
    ap.add_argument("--out", default=None, help="Output .xlsx path (default: <input-dir>/planning_<academy>.xlsx).")
    ap.add_argument("--debug", action="store_true",
                    help="Print per-athlete lookup trace (bracket found, page/mat, which priority matched).")
    ap.add_argument("--only", default=None,
                    help="When used with --debug, restrict the trace to athletes whose name contains this substring.")
    args = ap.parse_args()

    in_dir = Path(args.input_dir)
    participants = load_json(in_dir / "participants_by_team.json") or []
    planning     = load_json(in_dir / "planning.json") or []
    brackets     = load_json(in_dir / "brackets_by_category.json") or []

    athletes = collect_academy_athletes(participants, args.academy)
    if not athletes:
        print(f"[!] no athletes matched academy ~ '{args.academy}' in "
              "participants_by_team.json.", file=sys.stderr)
        return 1

    bracket_idx  = index_brackets(brackets)
    planning_idx = index_planning(planning)

    print(
        f"[+] {len(athletes)} athlete(s) for '{args.academy}' | "
        f"{len(bracket_idx)} bracket category(ies) | "
        f"{sum(len(v) for k, v in planning_idx.items() if k[1] is not None)} located planning slot(s).",
        file=sys.stderr,
    )

    debug_target = norm(args.only) if args.only else ""

    def trace(ath: dict, *parts: str) -> None:
        if not args.debug:
            return
        if debug_target and debug_target not in norm(ath.get("name", "")):
            return
        print("  " + " | ".join(parts), file=sys.stderr)

    rows: list[dict] = []
    unresolved = 0
    for ath in athletes:
        cat_key = norm(ath["category"])
        bracket = bracket_idx.get(cat_key)
        weight_limit = (bracket or {}).get("weight_limit") or ""
        belt = (bracket or {}).get("belt") or ath["category"].split(" - ", 1)[0]
        page_total = (bracket or {}).get("page_total")

        seed, page, bracket_mat, _ = locate_athlete_in_bracket(ath["name"], bracket)

        if args.debug and (not debug_target or debug_target in norm(ath.get("name", ""))):
            print(f"[{ath['name']}] team={ath['team']!r} cat={ath['category']!r} "
                  f"cat_key={cat_key!r} bracket_found={bracket is not None} "
                  f"page={page} mat={bracket_mat!r} seed={seed}",
                  file=sys.stderr)

        # Priority 1: exact match (category, page, mat).
        slot = None
        priority = None
        if page is not None and bracket_mat:
            key1 = (cat_key, page, norm(bracket_mat))
            p1_hits = planning_idx.get(key1, [])
            trace(ath, f"P1 key={key1}", f"hits={len(p1_hits)}")
            for s in p1_hits:
                slot = s; priority = 1; break
        # Priority 2: (category, page, any mat) — rare case of mat typo.
        if slot is None and page is not None:
            for (cat, pg, mat_k), slots in planning_idx.items():
                if cat == cat_key and pg == page and slots:
                    trace(ath, f"P2 any-mat match: (cat={cat_key!r}, page={page}, mat={mat_k!r})")
                    slot = slots[0]; priority = 2; break
        # Priority 3: single-page or single-slot category.
        if slot is None:
            broad = planning_idx.get((cat_key, None, None), [])
            if broad:
                # Prefer the one on the bracket mat, else the earliest.
                broad_sorted = sorted(
                    broad,
                    key=lambda s: (
                        0 if norm(s.get("mat")) == norm(bracket_mat) else 1,
                        s.get("date") or "", s.get("time") or "",
                    ),
                )
                slot = broad_sorted[0]
                priority = 3
                trace(ath, f"P3 broad fallback: {len(broad)} slot(s) for category, "
                           f"picked {slot.get('date')} {slot.get('time')} {slot.get('mat')} "
                           f"fight={slot.get('fight_index')}/{slot.get('fight_total')}")

        trace(ath, f"=> priority={priority} slot="
                   + (f"{slot.get('date')} {slot.get('time')} {slot.get('mat')} "
                      f"fight={slot.get('fight_index')}/{slot.get('fight_total')}"
                      if slot else "None"))

        if slot is None:
            unresolved += 1
            # Build a helpful note so the user can see *why* it's missing.
            hints = []
            if bracket is None:
                hints.append("catégorie absente des tableaux")
            elif page is None:
                hints.append("athlète non localisé dans le tableau")
            if not planning_idx.get((cat_key, None, None)):
                hints.append("catégorie absente du planning")
            rows.append({
                "name": ath["name"], "team": ath["team"],
                "category": ath["category"], "weight_limit": weight_limit,
                "seed": seed, "belt": belt,
                "date": "", "time": "", "mat": bracket_mat,
                "page": page, "page_total": page_total,
                "note": "; ".join(hints) or "pas de créneau trouvé",
            })
            continue

        rows.append({
            "name": ath["name"], "team": ath["team"],
            "category": ath["category"], "weight_limit": weight_limit,
            "seed": seed, "belt": belt,
            "date": slot.get("date") or "",
            "time": slot.get("time") or "",
            "mat":  slot.get("mat")  or bracket_mat,
            "page": slot.get("fight_index") if slot.get("fight_index") is not None else page,
            "page_total": slot.get("fight_total") if slot.get("fight_total") is not None else page_total,
            "note": slot.get("note") or "",
            "has_slot": True,
        })

    rows.sort(key=lambda r: (str(r["date"]), str(r["time"]), str(r["mat"]), str(r["name"])))

    slug = "".join(c if c.isalnum() else "_" for c in args.academy).strip("_")
    out_path = Path(args.out) if args.out else (in_dir / f"planning_{slug}.xlsx")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    write_xlsx(rows, out_path, args.academy)
    print(f"[+] wrote {len(rows)} row(s) ({unresolved} unresolved) -> {out_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
