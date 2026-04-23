# cfjjb-planning

Build a per-academy Excel fight-planning for any [CFJJB](https://cfjjb.com)
competition. The scraper drives the CFJJB site with a headless Chromium
browser, harvests the participants / planning / brackets tabs straight from
the rendered DOM, and joins the three views on `(category, page, mat)` so
every athlete ends up with their actual date, time, tatami and `N/M`
tableau number.

## Output

The builder produces one `.xlsx` per academy with these columns:

| Participant | Équipe | Catégorie | Date | Heure | Tapis | Tableaux | Note |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Jane DOE | Example Academy | BLANCHE - ADULTE - FEMME - LEVE | 2026-04-26 | 11:25 | Tatamis 6 | 7/9 | |
| John DOE | Example Academy | MARRON - MASTER 3/4 - HOMME - PESADISSIMO | 2026-04-25 | 14:17 | Tatamis 5 | 1/1 | P.Seule |

Rows are colour-tinted by belt, sorted by date → time → mat, and saved to
`output/planning_<academy>.xlsx`.

## Install

A Python 3.11+ virtual environment is recommended.

```bash
git clone https://github.com/SebastienGaspar/cfjjb-planning.git
cd cfjjb-planning

python3 -m venv .venv
source .venv/bin/activate            # on Windows PowerShell: .venv\Scripts\Activate.ps1

pip install --upgrade pip
pip install -r requirements.txt
playwright install chromium          # one-time: grabs the bundled browser
```

## Run

Two steps: extract, then build. The extractor writes all intermediate
JSON into `output/`; the builder reads from there.

### 1. Extract

`--competition` is required and accepts either the numeric id or any
`cfjjb.com` URL that carries it:

```bash
# Bare id
python3 extract_cfjjb.py --competition 941

# Or any of these URL forms
python3 extract_cfjjb.py --competition https://cfjjb.com/competitions/signup/info/941
python3 extract_cfjjb.py --competition "https://cfjjb.com/competition-group/470?tab=participants&id=941"

# Only refresh one tab
python3 extract_cfjjb.py --competition 941 --tabs brackets

# Watch the browser work
python3 extract_cfjjb.py --competition 941 --headed
```

After this step, `output/` contains:

| File | Source |
| --- | --- |
| `participants_by_team.json` | the "Liste des Participants" tab, `?by_team` |
| `planning.json` | the "Le Planning" tab |
| `brackets_by_category.json` | the "Tableaux de Combats" tab (aggregated per category) |
| `brackets_sections_raw.json` | one record per bracket section, with `outer_html` for debugging |
| `<tab>.html` and `<tab>.png` | rendered snapshot of each tab for debugging |

### 2. Build the Excel for your academy

```bash
python3 build_planning_xlsx.py --academy "Example Academy"
```

The academy match is a substring, accent- and case-insensitive, so
`"example"` or `"EXAMPLE ACADEMY"` work equally well. Output lands in
`output/planning_<academy>.xlsx`.

A `--debug` mode prints a one-line trace per athlete showing which
priority matched and why, with `--only "name-substring"` to focus it:

```bash
python3 build_planning_xlsx.py --academy "Example Academy" --debug --only "DOE"
```

## Helpers

Two tiny inspectors for the extracted JSON, handy for debugging or
sanity-checking a run:

```bash
python3 check_planning.py                           # list every category + slot count
python3 check_planning.py "Bleue - Adulte - Homme - Medio"

python3 check_brackets.py                           # summary by category
python3 check_brackets.py "Jane DOE"                # find a fighter
python3 check_brackets.py "Bleue - Adulte"          # find a category
```

## How it works (short version)

CFJJB is a Vue SPA with two particularly fun behaviours:

1. **Lazy mount** — bracket pages and planning rows only render when
   scrolled into view.
2. **Lazy unmount** — once they leave the viewport, Vue replaces them
   with a placeholder again.

A one-shot scrape therefore only sees what's currently on screen. This
project instead installs a `MutationObserver` inside the page that
caches every `<section class="wpage">` and `<li data-cate-fight>` the
instant it mounts (keyed so the entry is kept forever), then scrolls
the whole page top-to-bottom to force every lazy element to briefly
materialise. The cache is read at the end, guaranteeing complete data.

## Files

```
extract_cfjjb.py             # playwright scraper — writes the JSONs under output/
build_planning_xlsx.py       # reads the JSONs, writes the Excel
check_planning.py            # inspect output/planning.json
check_brackets.py            # inspect output/brackets_by_category.json
requirements.txt             # playwright + openpyxl
```

## License

MIT — see [LICENSE](LICENSE).
