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
# Bare id — writes to output/941/
python3 extract_cfjjb.py --competition 941

# Or any of these URL forms
python3 extract_cfjjb.py --competition https://cfjjb.com/competitions/signup/info/941
python3 extract_cfjjb.py --competition "https://cfjjb.com/competition-group/470?tab=participants&id=941"

# Only refresh one tab
python3 extract_cfjjb.py --competition 941 --tabs brackets

# Watch the browser work
python3 extract_cfjjb.py --competition 941 --headed

# Override the output location (default: output/<competition-id>/)
python3 extract_cfjjb.py --competition 941 --out-dir output/orleans_gi
```

Each run writes into its own `output/<competition-id>/` folder so that
multiple competitions of the same event (Gi, No-Gi, Kids Gi, Kids
No-Gi) can be extracted without overwriting each other.

After this step, `output/<competition-id>/` contains:

| File | Source |
| --- | --- |
| `participants_by_team.json` | the "Liste des Participants" tab, `?by_team` |
| `planning.json` | the "Le Planning" tab |
| `brackets_by_category.json` | the "Tableaux de Combats" tab (aggregated per category) |
| `brackets_sections_raw.json` | one record per bracket section, with `outer_html` for debugging |
| `meta.json` | the competition id, its page-visible name, and a derived `short_label` (`GI`, `NO GI`, `Kids GI`, `Kids NO GI`) |
| `<tab>.html` and `<tab>.png` | rendered snapshot of each tab for debugging |

### 2. Build the planning for your academy

```bash
# xlsx (default) — reads from output/941/
python3 build_planning_xlsx.py --academy "Example Academy" --input-dir output/941

# Same thing as a PDF
python3 build_planning_xlsx.py --academy "Example Academy" --input-dir output/941 --format pdf

# Pick the output basename (extension added automatically)
python3 build_planning_xlsx.py --academy "Example Academy" --input-dir output/941 --name orleans_gi
```

The academy match is a substring, accent- and case-insensitive, so
`"example"` or `"EXAMPLE ACADEMY"` work equally well. Output lands in
`<first-input-dir>/planning_<academy>.<xlsx|pdf>` by default; override
with `--name <stem>` (placed under the first input dir) or `--out
<path>`.

A `--debug` mode prints a one-line trace per athlete showing which
priority matched and why, with `--only "name-substring"` to focus it:

```bash
python3 build_planning_xlsx.py --academy "Example Academy" --input-dir output/941 --debug --only "DOE"
```

### 2b. Merge several competitions of the same event

Events on CFJJB are commonly split across four competition ids — Gi,
No-Gi, Kids Gi, Kids No-Gi. After extracting each one into its own
folder, pass them all to the builder in a single run:

```bash
python3 extract_cfjjb.py --competition 941      # Gi        -> output/941/
python3 extract_cfjjb.py --competition 942      # No-Gi     -> output/942/
python3 extract_cfjjb.py --competition 943      # Kids Gi   -> output/943/
python3 extract_cfjjb.py --competition 944      # Kids No-Gi -> output/944/

python3 build_planning_xlsx.py \
    --academy "Example Academy" \
    --input-dir output/941 output/942 output/943 output/944 \
    --format pdf --name orleans_2026
```

The join runs independently per directory (category/page/mat keys only
make sense within one competition), then every row is merged and
re-sorted by date → time → mat. A `Compétition` column is added to the
output whenever more than one `--input-dir` is given.

The label shown in that column is derived, in order of precedence:

1. An explicit override via `--input-dir path=LABEL`, e.g.
   `--input-dir output/941=GI output/942="NO GI"`.
2. The `short_label` written into `meta.json` by the extractor (derived
   from the page-visible competition name: `Kids` prefix if "KIDS"
   appears, suffix `NO GI` if "NO GI" / "NOGI" appears, else `GI`).
3. The directory basename (fallback for legacy extractions without
   `meta.json`).

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
