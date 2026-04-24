#!/usr/bin/env python3
"""
Headless-browser extractor for cfjjb.com competition id=941, group=470.

Uses Playwright (Chromium) to render the Vue SPA and drive each tab
(participants / plannings / brackets). For every tab it writes the
rendered HTML + screenshot for debugging, then runs a dedicated DOM
scraper that produces the three structured JSON files consumed by
build_planning_xlsx.py:

    output/participants_by_team.json
    output/planning.json
    output/brackets_by_category.json

Academy filtering is a downstream concern and lives in
build_planning_xlsx.py --academy "...".

Setup (once):
    pip install playwright
    playwright install chromium

Run:
    python3 extract_cfjjb.py --competition 941
    python3 extract_cfjjb.py --competition https://cfjjb.com/competitions/signup/info/941
    python3 extract_cfjjb.py --competition 941 --tabs brackets
    python3 extract_cfjjb.py --competition 941 --headed   # watch it work
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from pathlib import Path

from urllib.parse import urljoin, urlparse, urlunparse, parse_qsl, urlencode

from playwright.sync_api import (
    Page,
    Response,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

BASE = "https://cfjjb.com"


def signup_info_url(competition_id: int) -> str:
    return f"{BASE}/competitions/signup/info/{competition_id}"


def parse_competition(value: str) -> int:
    """Accept either a bare numeric id ("941") or any cfjjb.com URL that
    carries the competition id in its path or query string:
      - https://cfjjb.com/competitions/signup/info/941
      - https://cfjjb.com/competition-group/470?tab=participants&id=941
      - cfjjb.com/...?id=941
    Returns the numeric id. Raises argparse.ArgumentTypeError on failure."""
    import argparse as _ap
    import re as _re
    s = (value or "").strip()
    if not s:
        raise _ap.ArgumentTypeError("empty --competition value")
    if s.isdigit():
        return int(s)
    # Try URL forms.
    parts = urlparse(s if "://" in s else "https://" + s)
    if parts.netloc and "cfjjb.com" not in parts.netloc.lower():
        raise _ap.ArgumentTypeError(
            f"URL host must be cfjjb.com (got {parts.netloc!r})"
        )
    # Path: .../signup/info/<id>
    m = _re.search(r"/signup/info/(\d+)", parts.path)
    if m:
        return int(m.group(1))
    # Query: ?id=<n>
    q = dict(parse_qsl(parts.query))
    if q.get("id", "").isdigit():
        return int(q["id"])
    raise _ap.ArgumentTypeError(
        f"could not find a competition id in {value!r}. Pass either a "
        "bare id (e.g. 941) or a full cfjjb.com competition URL."
    )


def discover_tab_links(page: Page, competition_id: int) -> dict[str, str]:
    """Visit the public signup-info page and read the three tab links straight
    off the #list_participants_button / #planning_button / #brackets_button
    anchors. Returns absolute URLs keyed by tab name."""
    url = signup_info_url(competition_id)
    print(f"[+] discovering tab links from {url}", file=sys.stderr)
    page.goto(url, wait_until="networkidle", timeout=60_000)

    hrefs = page.evaluate(r"""
        () => {
          const pick = (id) => {
            const a = document.getElementById(id);
            return a ? a.getAttribute("href") : null;
          };
          return {
            participants: pick("list_participants_button"),
            plannings:    pick("planning_button"),
            brackets:     pick("brackets_button"),
          };
        }
    """)

    resolved: dict[str, str] = {}
    for tab, href in (hrefs or {}).items():
        if not href:
            raise RuntimeError(f"tab link not found on signup page: {tab}")
        abs_url = urljoin(BASE + "/", href)
        # Pre-select "by team" on participants so the DOM is already grouped.
        if tab == "participants":
            parts = urlparse(abs_url)
            q = dict(parse_qsl(parts.query, keep_blank_values=True))
            q.setdefault("by_team", "")
            abs_url = urlunparse(parts._replace(query=urlencode(q)))
        resolved[tab] = abs_url
        print(f"    {tab}: {abs_url}", file=sys.stderr)
    return resolved


# ---------- core ----------

def capture_tab(page: Page, tab: str, url: str, out_dir: Path) -> dict:
    """Load a tab, wait for it to settle, save rendered HTML + screenshot.

    The data we care about is scraped from the DOM by the per-tab scrapers
    (participants/planning/brackets); this function only drives the page
    load and persists the page state for debugging.
    """
    print(f"[+] {tab}: GET {url}", file=sys.stderr)
    page.goto(url, wait_until="networkidle", timeout=60_000)

    try:
        page.wait_for_load_state("networkidle", timeout=15_000)
    except PlaywrightTimeoutError:
        pass
    page.wait_for_timeout(1500)

    (out_dir / f"{tab}.html").write_text(page.content(), encoding="utf-8")
    page.screenshot(path=str(out_dir / f"{tab}.png"), full_page=True)
    print(f"    saved rendered HTML + screenshot for '{tab}'", file=sys.stderr)
    return {"tab": tab, "page_url": url}


def scrape_participants_by_team(page: Page) -> list[dict]:
    """Walk the rendered DOM on the participants-by-team view and return
    [{team, athletes:[{name, category, belt?, weight?}, ...]}, ...].

    Robust to the site's current markup: for each candidate team block we
    collect every text row that looks like an athlete entry and split it
    into (name, category) when a clear separator is present.
    """
    # Observed structure on ?by_team:
    #   <div class="flex items-center justify-between mt-10 mb-2">
    #     <h1 class="text-2xl font-bold text-blue-800 uppercase tracking-wider">ACADEMIE ...</h1>
    #     <div class="uppercase text-gray-500 text-xs">Total : <b class="text-xl">3</b></div>
    #   </div>
    #   <table class="w-full shadow-lg">
    #     <tbody>
    #       <tr class="border-gray-200 border-t">
    #         <td class="... uppercase">Blanche - Adulte - Homme - Pesadissimo</td>
    #         <td class="... capitalize ... font-bold">Jane DOE</td>
    #       </tr>
    #       ...
    #     </tbody>
    #   </table>
    js = r"""
    () => {
      const clean = (s) => (s || "").replace(/\s+/g, " ").trim();

      // Return the team name for a given table: the nearest <h1> with
      // `text-blue-800` that sits *before* the table in document order.
      const teamNameFor = (table) => {
        const headers = Array.from(document.querySelectorAll("h1.text-blue-800, h1.text-2xl.text-blue-800"));
        let last = "";
        for (const h of headers) {
          if (h.compareDocumentPosition(table) & Node.DOCUMENT_POSITION_FOLLOWING) {
            last = clean(h.innerText);
          } else {
            break;
          }
        }
        return last;
      };

      // The "Total" indicator sitting next to the <h1>.
      const totalFor = (table) => {
        const headers = Array.from(document.querySelectorAll("h1.text-blue-800"));
        let target = null;
        for (const h of headers) {
          if (h.compareDocumentPosition(table) & Node.DOCUMENT_POSITION_FOLLOWING) target = h;
          else break;
        }
        if (!target) return null;
        const bar = target.parentElement;
        if (!bar) return null;
        const b = bar.querySelector("b");
        const n = b ? parseInt(clean(b.innerText), 10) : NaN;
        return Number.isFinite(n) ? n : null;
      };

      const parseCategory = (raw) => {
        const parts = raw.split(/\s*-\s*/).map(clean).filter(Boolean);
        const out = { raw };
        if (parts[0]) out.belt = parts[0];
        if (parts[1]) out.age = parts[1];
        if (parts[2]) out.gender = parts[2];
        if (parts[3]) out.weight = parts.slice(3).join(" - ");
        return out;
      };

      const byTeam = new Map();
      const tables = Array.from(document.querySelectorAll("table.shadow-lg"));
      for (const table of tables) {
        const team = teamNameFor(table) || "(équipe inconnue)";
        const total = totalFor(table);
        const rows = Array.from(table.querySelectorAll("tbody > tr"));
        const athletes = [];
        for (const tr of rows) {
          const tds = tr.querySelectorAll(":scope > td");
          if (tds.length < 2) continue;
          const catText = clean(tds[0].innerText);
          const name    = clean(tds[1].innerText);
          if (!name) continue;
          athletes.push({
            name,
            category: catText,
            ...parseCategory(catText),
          });
        }
        if (!athletes.length) continue;
        if (!byTeam.has(team)) byTeam.set(team, { team, total, athletes: [] });
        byTeam.get(team).athletes.push(...athletes);
      }
      return Array.from(byTeam.values());
    }
    """
    try:
        return page.evaluate(js) or []
    except Exception as e:
        print(f"[!] participants DOM scrape failed: {e}", file=sys.stderr)
        return []


def scrape_planning(page: Page) -> list[dict]:
    """Walk the rendered DOM on the planning tab.

    Each fight is an <li> carrying data-* attributes:
      data-date="2026-04-25"
      data-area="Tatamis 1"
      data-cate-fight="6306"     (bracket / category identifier)
      class="... border-belt-<color> ..."
    and contains:
      - time in the first inner <div> ("10:50")
      - category text ("Bleue - Adulte - Homme - Medio")
      - a progress pill "N / M" (fight index / total), or "P.Seule" when
        the athlete is alone in the category.

    Day headers are <h4 class="... text-black"> before each group.
    Mat headers are <li class="... text-blue-900"> (no data-* attrs).

    Returns a flat list of fight records.
    """
    js = r"""
    () => {
      const clean = (s) => (s || "").replace(/\s+/g, " ").trim();

      const parseCategory = (raw) => {
        const parts = (raw || "").split(/\s*-\s*/).map(clean).filter(Boolean);
        const out = { raw: raw || "" };
        if (parts[0]) out.belt = parts[0];
        if (parts[1]) out.age = parts[1];
        if (parts[2]) out.gender = parts[2];
        if (parts[3]) out.weight = parts.slice(3).join(" - ");
        return out;
      };

      const beltFromClass = (el) => {
        const m = (el.className || "").match(/border-belt-([a-z]+)/i);
        return m ? m[1].toLowerCase() : null;
      };

      // Associate each fight <li> with the nearest preceding <h4> (date label).
      const dayHeaders = Array.from(document.querySelectorAll("h4"));
      const dayLabelFor = (el) => {
        let label = "";
        for (const h of dayHeaders) {
          if (h.compareDocumentPosition(el) & Node.DOCUMENT_POSITION_FOLLOWING) {
            label = clean(h.innerText);
          } else break;
        }
        return label;
      };

      const fights = [];
      const lis = Array.from(document.querySelectorAll("li[data-cate-fight]"));
      for (const li of lis) {
        const dateISO = li.getAttribute("data-date") || "";
        const mat     = li.getAttribute("data-area") || "";
        const cateFightId = li.getAttribute("data-cate-fight") || "";

        // time: the leaf <div> whose text is HH:MM
        let time = "";
        for (const d of li.querySelectorAll("div")) {
          if (d.querySelector("div, span")) continue;     // leaf only
          const t = clean(d.innerText);
          if (/^\d{1,2}:\d{2}$/.test(t)) { time = t; break; }
        }

        // category: the leaf <div> that is exactly "Belt - Age - Gender - Weight".
        // It sits next to a "N/M" pill inside a flex wrapper; picking the wrapper
        // would concatenate both texts — we must pick the leaf.
        let category = "";
        for (const d of li.querySelectorAll("div")) {
          if (d.querySelector("div, span")) continue;     // leaf only
          const t = clean(d.innerText);
          if (/^\d{1,2}:\d{2}$/.test(t)) continue;        // skip the time div
          // Category format: "Belt - Age - Gender - Weight(s)" → exactly 3 " - ".
          const segments = t.split(/\s-\s/);
          if (segments.length >= 4 && /^[A-Za-zÀ-ÿ]/.test(t)) {
            category = t;
            break;
          }
        }

        // fight progress: the "N / M" pill (rounded-md with nested spans)
        let fightIndex = null, fightTotal = null, note = null;
        const pill = li.querySelector("div.rounded-md");
        if (pill) {
          const spans = Array.from(pill.querySelectorAll("span"))
            .map(s => clean(s.innerText));
          const nums = spans.filter(s => /^\d+$/.test(s)).map(Number);
          if (nums.length >= 2) { fightIndex = nums[0]; fightTotal = nums[1]; }
          const txt = clean(pill.innerText);
          if (!/^\d+\s*\/\s*\d+$/.test(txt)) note = txt;
        }

        const rec = {
          date: dateISO,
          date_label: dayLabelFor(li),
          mat,
          time,
          category,
          ...parseCategory(category),
          belt_color: beltFromClass(li),
          cate_fight_id: cateFightId,
          fight_index: fightIndex,
          fight_total: fightTotal,
          note,
        };
        fights.push(rec);
      }
      return fights;
    }
    """
    try:
        return page.evaluate(js) or []
    except Exception as e:
        print(f"[!] planning DOM scrape failed: {e}", file=sys.stderr)
        return []


_PLANNING_OBSERVER_JS = r"""
() => {
  if (window.__CFJJB_PLANNING__) return;
  const cache = new Map();  // cate_fight_id -> record
  window.__CFJJB_PLANNING__ = cache;

  const clean = (s) => (s || "").replace(/\s+/g, " ").trim();

  const scrapeLi = (li) => {
    const id = li.getAttribute("data-cate-fight");
    if (!id) return null;

    let time = "";
    for (const d of li.querySelectorAll("div")) {
      if (d.querySelector("div, span")) continue;
      const t = clean(d.innerText);
      if (/^\d{1,2}:\d{2}$/.test(t)) { time = t; break; }
    }

    let category = "";
    for (const d of li.querySelectorAll("div")) {
      if (d.querySelector("div, span")) continue;
      const t = clean(d.innerText);
      if (/^\d{1,2}:\d{2}$/.test(t)) continue;
      const segs = t.split(/\s-\s/);
      if (segs.length >= 4 && /^[A-Za-zÀ-ÿ]/.test(t)) {
        category = t; break;
      }
    }

    let fightIndex = null, fightTotal = null, note = null;
    const pill = li.querySelector("div.rounded-md");
    if (pill) {
      const spans = Array.from(pill.querySelectorAll("span")).map(s => clean(s.innerText));
      const nums = spans.filter(s => /^\d+$/.test(s)).map(Number);
      if (nums.length >= 2) { fightIndex = nums[0]; fightTotal = nums[1]; }
      const txt = clean(pill.innerText);
      if (!/^\d+\s*\/\s*\d+$/.test(txt)) note = txt;
    }

    const bcm = (li.className || "").match(/border-belt-([a-z]+)/i);

    return {
      date: li.getAttribute("data-date") || "",
      date_label: "",
      mat: li.getAttribute("data-area") || "",
      time,
      category,
      belt: "", age: "", gender: "", weight: "", raw: category,
      belt_color: bcm ? bcm[1].toLowerCase() : null,
      cate_fight_id: id,
      fight_index: fightIndex,
      fight_total: fightTotal,
      note,
    };
  };

  const captureAll = () => {
    const headings = Array.from(document.querySelectorAll("h4"));
    const labelFor = (el) => {
      let lab = "";
      for (const h of headings) {
        if (h.compareDocumentPosition(el) & Node.DOCUMENT_POSITION_FOLLOWING) {
          lab = clean(h.innerText);
        } else break;
      }
      return lab;
    };
    for (const li of document.querySelectorAll("li[data-cate-fight]")) {
      const data = scrapeLi(li);
      if (!data) continue;
      data.date_label = labelFor(li);
      // Once a slot is captured, keep it forever.
      if (!cache.has(data.cate_fight_id)) cache.set(data.cate_fight_id, data);
    }
  };

  captureAll();

  const tryCapture = (el) => {
    if (!el || el.nodeType !== 1) return;
    if ((el.matches && el.matches("li[data-cate-fight]")) ||
        (el.querySelector && el.querySelector("li[data-cate-fight]"))) {
      captureAll();
    }
  };

  const obs = new MutationObserver((mutations) => {
    for (const m of mutations) {
      for (const added of m.addedNodes) tryCapture(added);
    }
  });
  obs.observe(document.body, { childList: true, subtree: true });
  window.__CFJJB_PLANNING_OBSERVER__ = obs;
  window.__CFJJB_PLANNING_POLL__ = setInterval(captureAll, 150);
}
"""


def harvest_planning(page: Page) -> list[dict]:
    """Same observer-first pattern as the brackets tab: install a
    capturing observer, scroll end-to-end to force any lazy-mounted
    `<li data-cate-fight>` rows to materialise, then read the cache."""
    page.evaluate(_PLANNING_OBSERVER_JS)

    page.evaluate("window.scrollTo(0, 0)")
    page.wait_for_timeout(400)
    viewport = page.evaluate("window.innerHeight") or 720
    step = max(int(viewport * 0.8), 500)

    y = 0
    stable = 0
    prev_sig: tuple | None = None
    for _ in range(150):
        h = page.evaluate("document.body.scrollHeight")
        cached = page.evaluate("window.__CFJJB_PLANNING__.size")
        sig = (y, h, cached)
        if sig == prev_sig:
            stable += 1
        else:
            stable = 0
        prev_sig = sig
        at_bottom = y >= max(0, h - viewport)
        if at_bottom and stable >= 3:
            break
        if stable >= 6:
            break
        y = min(y + step, h)
        page.evaluate(f"window.scrollTo(0, {y})")
        page.wait_for_timeout(250)

    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    page.wait_for_timeout(600)

    fights = page.evaluate(
        "() => Array.from(window.__CFJJB_PLANNING__.values())"
    ) or []
    print(
        f"    harvested {len(fights)} planning slot(s) via MutationObserver",
        file=sys.stderr,
    )
    return fights


_BRACKET_OBSERVER_JS = r"""
() => {
  if (window.__CFJJB_BRACKETS__) return;  // install once

  const cache = new Map();           // "category||page" -> scraped entry
  const emptyCats = new Set();       // category names that were still placeholders
  window.__CFJJB_BRACKETS__ = cache;
  window.__CFJJB_BRACKET_EMPTY__ = emptyCats;

  const clean = (s) => (s || "").replace(/\s+/g, " ").trim();
  const isLeaf = (el) => !el.querySelector("div, span, h1, h2, h3, ul, li, a, button");

  const scrapeSection = (sec) => {
    const h2 = sec.querySelector("header h2");
    if (!h2) return null;
    const cat = clean(h2.innerText);
    if (!cat) return null;

    let page = null, page_total = null;
    for (const d of sec.querySelectorAll("header div")) {
      if (!isLeaf(d)) continue;
      const m = clean(d.innerText).match(/^page\s+(\d+)\s*\/\s*(\d+)$/i);
      if (m) { page = +m[1]; page_total = +m[2]; break; }
    }
    if (page === null) return null;

    let mat = "";
    for (const li of sec.querySelectorAll("header ul li")) {
      const t = clean(li.innerText);
      if (/^tatamis?\s+\d+/i.test(t)) { mat = t; break; }
    }

    const weightEl = h2.parentElement ? h2.parentElement.querySelector("div.text-base") : null;
    const weight_limit = weightEl ? clean(weightEl.innerText) : "";
    const countEl = sec.querySelector("header li span.text-3xl");
    const combatants_total = countEl
      ? (parseInt(clean(countEl.innerText), 10) || null)
      : null;

    const fighters = [];
    const seen = new Set();
    for (const card of sec.querySelectorAll("div[id^='ins_']")) {
      const id = card.getAttribute("id") || "";
      if (!id || id === "ins_undefined") continue;
      const nameEl = card.querySelector("span.font-bold");
      const name = clean(nameEl ? nameEl.innerText : "");
      if (!name) continue;
      const teamEl = card.querySelector("div.font-thin");
      const team = clean(teamEl ? teamEl.innerText : "");
      const seedEl = card.querySelector(".placement .w-5");
      const seedTxt = clean(seedEl ? seedEl.innerText : "");
      const seed = seedTxt ? (parseInt(seedTxt, 10) || null) : null;
      const ins_id = id.replace(/^ins_/, "");
      const key = ins_id + "|" + name;
      if (seen.has(key)) continue;
      seen.add(key);
      fighters.push({
        ins_id, seed, name, team,
        bracket_match_id: card.getAttribute("data-parent") || null,
      });
    }

    return {
      category: cat, page, page_total, mat, weight_limit, combatants_total,
      fighters,
      section_id: sec.getAttribute("id") || null,
      outer_html: sec.outerHTML,  // kept for debugging; stripped before final JSON
    };
  };

  const captureAll = () => {
    for (const sec of document.querySelectorAll("section.wpage")) {
      const data = scrapeSection(sec);
      if (!data) continue;
      const key = data.category + "||" + data.page;
      const prev = cache.get(key);
      if (!prev || data.fighters.length > prev.fighters.length) {
        cache.set(key, data);
      }
    }
    // Track categories that are still empty placeholders.
    for (const it of document.querySelectorAll("#bracket-pages div.h-screen > i")) {
      const txt = clean(it.innerText || it.textContent || "");
      if (txt) emptyCats.add(txt);
    }
  };

  captureAll();

  const tryCapture = (el) => {
    if (!el || el.nodeType !== 1) return;
    if (el.matches && el.matches("section.wpage")) {
      const data = scrapeSection(el);
      if (data) {
        const key = data.category + "||" + data.page;
        const prev = cache.get(key);
        if (!prev || data.fighters.length > prev.fighters.length) {
          cache.set(key, data);
        }
      }
    }
    if (el.querySelectorAll) {
      for (const sec of el.querySelectorAll("section.wpage")) tryCapture(sec);
    }
  };

  // Capture synchronously on every added node — we can't afford to wait
  // one RAF if Vue unmounts on the very next microtask.
  const obs = new MutationObserver((mutations) => {
    for (const m of mutations) {
      for (const added of m.addedNodes) tryCapture(added);
    }
  });
  obs.observe(document.body, { childList: true, subtree: true });
  window.__CFJJB_BRACKET_OBSERVER__ = obs;

  // Periodic full rescan as a safety net (cheap: each pass is bounded by
  // the number of currently mounted sections).
  window.__CFJJB_BRACKET_POLL__ = setInterval(captureAll, 150);
}
"""


def harvest_brackets(page: Page, out_dir: Path) -> list[dict]:
    """Install an in-page MutationObserver that captures every <section
    class="wpage"> the moment it mounts — we then scroll top-to-bottom
    once so every lazy-mounted section briefly exists, and read the
    cache at the end.

    The observer-first approach is crucial: the brackets tab both mounts
    AND *unmounts* sections as they enter/leave the viewport, and a
    single synchronous scrape from Python only sees what's on screen.
    """
    page.evaluate(_BRACKET_OBSERVER_JS)

    page.evaluate("window.scrollTo(0, 0)")
    page.wait_for_timeout(500)
    viewport = page.evaluate("window.innerHeight") or 720
    step = max(int(viewport * 0.7), 400)

    y = 0
    last_sig: tuple | None = None
    stable = 0
    for _ in range(400):
        h = page.evaluate("document.body.scrollHeight")
        ph = page.evaluate("document.querySelectorAll('#bracket-pages .h-screen').length")
        cached = page.evaluate("window.__CFJJB_BRACKETS__.size")
        sig = (y, h, ph, cached)
        if sig == last_sig:
            stable += 1
        else:
            stable = 0
        last_sig = sig
        at_bottom = y >= max(0, h - viewport)
        if at_bottom and ph == 0 and stable >= 3:
            break
        if stable >= 8:  # safety: nothing is changing anymore
            break
        new_y = min(y + step, h)
        if new_y == y:
            y = 0  # bounce back to top for one more sweep
        else:
            y = new_y
        page.evaluate(f"window.scrollTo(0, {y})")
        page.wait_for_timeout(300)

    # One long pause at the very bottom so the final few sections mount.
    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    page.wait_for_timeout(800)

    raw_entries: list[dict] = page.evaluate(
        "() => Array.from(window.__CFJJB_BRACKETS__.values())"
    ) or []
    empty_cats: list[str] = page.evaluate(
        "() => Array.from(window.__CFJJB_BRACKET_EMPTY__ || [])"
    ) or []

    # Persist the raw per-section capture (with HTML) for debugging. If
    # anything ever looks wrong again, open this file and grep for the
    # fighter / category in question.
    (out_dir / "brackets_sections_raw.json").write_text(
        json.dumps(raw_entries, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        f"    captured {len(raw_entries)} section HTML snapshot(s) "
        f"-> {out_dir}/brackets_sections_raw.json",
        file=sys.stderr,
    )

    # Strip HTML from the entries we aggregate (keeps the production JSON
    # small and diffable).
    for e in raw_entries:
        e.pop("outer_html", None)

    # Aggregate cached section records by *normalized* category, because
    # the mounted <h2> (uppercase via CSS) and the lazy-placeholder <i>
    # (mixed case from textContent) produce different raw strings for
    # the same category.  Keying by the normalized form prevents a later
    # empty-placeholder record from overwriting a populated one.
    def _norm_cat(s: str) -> str:
        t = unicodedata.normalize("NFKD", s or "")
        t = "".join(c for c in t if not unicodedata.combining(c))
        return " ".join(t.lower().split())

    def split_cat(cat: str) -> dict:
        parts = [p.strip() for p in cat.split(" - ")]
        return {
            "raw": cat,
            "belt": parts[0] if len(parts) > 0 else "",
            "age": parts[1] if len(parts) > 1 else "",
            "gender": parts[2] if len(parts) > 2 else "",
            "weight_class": " - ".join(parts[3:]) if len(parts) > 3 else "",
        }

    by_cat: dict[str, dict] = {}  # keyed by _norm_cat(cat)
    for e in raw_entries:
        cat = e.get("category") or ""
        if not cat:
            continue
        key = _norm_cat(cat)
        dst = by_cat.setdefault(key, {
            **split_cat(cat),
            "weight_limit": "",
            "combatants_total": None,
            "page_total": None,
            "pages_detail": [],
            "status": "populated",
        })
        if e.get("weight_limit") and not dst["weight_limit"]:
            dst["weight_limit"] = e["weight_limit"]
        if e.get("combatants_total") and not dst["combatants_total"]:
            dst["combatants_total"] = e["combatants_total"]
        if e.get("page_total") and not dst["page_total"]:
            dst["page_total"] = e["page_total"]
        dst["pages_detail"].append({
            "page": e["page"],
            "page_total": e.get("page_total"),
            "mat": e.get("mat", ""),
            "fighters": e.get("fighters", []) or [],
        })

    out: list[dict] = []
    for entry in by_cat.values():
        entry["pages_detail"].sort(key=lambda p: p.get("page") or 0)
        if not entry["page_total"] and entry["pages_detail"]:
            entry["page_total"] = max(
                (p.get("page_total") or 0) for p in entry["pages_detail"]
            ) or len(entry["pages_detail"])
        seen: set = set()
        fighters: list[dict] = []
        for pd in entry["pages_detail"]:
            for f in pd.get("fighters", []) or []:
                k = (f.get("ins_id"), f.get("name"))
                if k in seen:
                    continue
                seen.add(k)
                fighters.append({**f, "page": pd.get("page"), "mat": pd.get("mat")})
        fighters.sort(key=lambda f: (f.get("seed") if f.get("seed") is not None else 1e9))
        entry["fighters"] = fighters
        out.append(entry)

    # Add any category that never rendered (still a placeholder). Dedup
    # against the populated set using the *normalized* form to avoid
    # creating a phantom empty twin of a populated category.
    known_norm = {_norm_cat(e["raw"]) for e in out}
    for cat in empty_cats:
        if _norm_cat(cat) in known_norm:
            continue
        out.append({
            **split_cat(cat),
            "weight_limit": "",
            "combatants_total": None,
            "page_total": None,
            "pages_detail": [],
            "fighters": [],
            "status": "empty",
        })

    populated = sum(1 for e in out if e["status"] == "populated")
    pages_total = sum(len(e["pages_detail"]) for e in out)
    print(
        f"    harvested {len(out)} categor(ies) ({populated} populated) / "
        f"{pages_total} bracket page(s) via MutationObserver",
        file=sys.stderr,
    )
    return out


def scrape_brackets(page: Page) -> list[dict]:
    """Walk the brackets tab DOM and return one record per category.

    Each category is rendered as one or more <section class="wpage">.
    Header:
      <h2>Blanche - Juvénile - Homme - Galo</h2>
      <div class="text-base">Jusqu'à 53.5kg</div>
      <li><span class="text-3xl">3</span> combattants</li>
      <li><span>Tatamis 6</span></li>
      <div>Page 1/1</div>
    Fighter entries:
      <div id="ins_137549" data-parent="163621">
        <div class="placement">1</div>
        <span class="font-bold">John DOE</span>
        <div class="font-thin">Z-Team</div>
      </div>
    Categories can span multiple pages — we merge by category name.
    Empty categories render as: <i style="display:none">Cat name</i> inside <div.h-screen>.
    """
    js = r"""
    () => {
      const clean = (s) => (s || "").replace(/\s+/g, " ").trim();

      const parseCategory = (raw) => {
        const parts = (raw || "").split(/\s*-\s*/).map(clean).filter(Boolean);
        return {
          raw: raw || "",
          belt: parts[0] || "",
          age: parts[1] || "",
          gender: parts[2] || "",
          weight_class: parts.slice(3).join(" - ") || "",
        };
      };

      const byCategory = new Map();

      const ensure = (catRaw) => {
        if (!byCategory.has(catRaw)) {
          byCategory.set(catRaw, {
            ...parseCategory(catRaw),
            weight_limit: "",
            combatants_total: null,
            page_total: 0,
            pages_detail: [],   // one entry per <section class="wpage">
            status: "empty",
          });
        }
        return byCategory.get(catRaw);
      };

      const parsePage = (txt) => {
        const m = clean(txt).match(/page\s*(\d+)\s*\/\s*(\d+)/i);
        return m ? { page: parseInt(m[1], 10), page_total: parseInt(m[2], 10) } : null;
      };

      const isLeaf = (el) => !el.querySelector("div, span, h1, h2, h3, ul, li, a, button");

      // 1) Populated bracket pages.
      const sections = Array.from(document.querySelectorAll("section.wpage"));
      for (const sec of sections) {
        const h2 = sec.querySelector("header h2");
        if (!h2) continue;
        const catRaw = clean(h2.innerText);
        if (!catRaw) continue;
        const entry = ensure(catRaw);
        entry.status = "populated";

        // Weight limit: <div class="text-base"> sibling of h2's parent.
        const weightDiv = h2.parentElement?.querySelector("div.text-base");
        if (weightDiv) {
          const t = clean(weightDiv.innerText);
          if (t) entry.weight_limit = t;
        }

        // Combatants count (first <li> with a big <span>).
        const countSpan = sec.querySelector("header li span.text-3xl");
        if (countSpan) {
          const n = parseInt(clean(countSpan.innerText), 10);
          if (Number.isFinite(n)) entry.combatants_total = n;
        }

        // Page X/Y info: leaf <div> whose text is exactly "Page N/M".
        let pageInfo = null;
        for (const d of sec.querySelectorAll("header div")) {
          if (!isLeaf(d)) continue;
          const t = clean(d.innerText);
          if (/^page\s+\d+\s*\/\s*\d+$/i.test(t)) {
            pageInfo = parsePage(t);
            if (pageInfo) break;
          }
        }

        // Mat: the <li> whose text starts with "Tatami(s) N" (avoids picking up
        // the neighbouring "combattants" / hidden "combats" <li>).
        let mat = "";
        for (const li of sec.querySelectorAll("header ul li")) {
          const t = clean(li.innerText);
          if (/^tatamis?\s+\d+/i.test(t)) { mat = t; break; }
        }

        // Fighters visible on THIS page.
        const fighters = [];
        const seenOnPage = new Set();
        for (const card of sec.querySelectorAll("div[id^='ins_']")) {
          const id = card.getAttribute("id") || "";
          if (!id || id === "ins_undefined") continue;
          const nameEl = card.querySelector("span.font-bold");
          const name = clean(nameEl?.innerText || "");
          if (!name) continue;          // placement-only cell (later rounds)
          const teamEl = card.querySelector("div.font-thin");
          const team = clean(teamEl?.innerText || "");
          const seedEl = card.querySelector("span.placement .w-5, .placement .w-5");
          const seedTxt = clean(seedEl?.innerText || "");
          const seed = seedTxt ? parseInt(seedTxt, 10) : null;
          const insId = id.replace(/^ins_/, "");
          const key = insId + "|" + name;
          if (seenOnPage.has(key)) continue;
          seenOnPage.add(key);
          fighters.push({
            ins_id: insId,
            seed: Number.isFinite(seed) ? seed : null,
            name,
            team,
            bracket_match_id: card.getAttribute("data-parent") || null,
          });
        }

        entry.pages_detail.push({
          page: pageInfo?.page ?? null,
          page_total: pageInfo?.page_total ?? null,
          mat,
          fighters: fighters.sort((a, b) => (a.seed ?? 1e9) - (b.seed ?? 1e9)),
        });
        if (pageInfo?.page_total) entry.page_total = pageInfo.page_total;
      }

      // 2) Empty-category placeholders: <i style="display:none">Cat</i>.
      for (const it of document.querySelectorAll("div.h-screen > i")) {
        const catRaw = clean(it.innerText || it.textContent || "");
        if (!catRaw) continue;
        ensure(catRaw); // stays "empty"
      }

      // Flatten an all-fighters list (convenience) + sort pages.
      const result = [];
      for (const entry of byCategory.values()) {
        entry.pages_detail.sort((a, b) => (a.page ?? 1e9) - (b.page ?? 1e9));
        if (!entry.page_total && entry.pages_detail.length)
          entry.page_total = entry.pages_detail.length;
        const seen = new Set();
        entry.fighters = [];
        for (const pd of entry.pages_detail) {
          for (const f of pd.fighters) {
            const k = f.ins_id + "|" + f.name;
            if (seen.has(k)) continue;
            seen.add(k);
            entry.fighters.push({ ...f, page: pd.page, mat: pd.mat });
          }
        }
        entry.fighters.sort((a, b) => (a.seed ?? 1e9) - (b.seed ?? 1e9));
        result.push(entry);
      }
      return result;
    }
    """
    try:
        return page.evaluate(js) or []
    except Exception as e:
        print(f"[!] brackets DOM scrape failed: {e}", file=sys.stderr)
        return []


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--competition", required=True, type=parse_competition,
                    metavar="ID_OR_URL",
                    help="Competition id (e.g. 941) or any cfjjb.com URL "
                         "containing the id (signup/info/<id> or ?id=<n>).")
    ap.add_argument("--tabs", default="participants,plannings,brackets",
                    help="Comma-separated tabs (default: all three).")
    ap.add_argument("--headed", action="store_true", help="Run browser in headed mode.")
    ap.add_argument("--out-dir", default=None,
                    help="Directory for the JSON + HTML/PNG dumps "
                         "(default: output/<competition-id>/). Useful when "
                         "extracting multiple competitions of the same event.")
    args = ap.parse_args()

    if args.out_dir:
        out_dir = Path(args.out_dir)
    else:
        out_dir = Path(__file__).parent / "output" / str(args.competition)
    out_dir.mkdir(parents=True, exist_ok=True)

    tabs = [t.strip() for t in args.tabs.split(",") if t.strip()]

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not args.headed)
        ctx = browser.new_context(
            locale="fr-FR",
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            ),
        )
        page = ctx.new_page()

        tab_urls = discover_tab_links(page, args.competition)

        results: dict[str, dict] = {}
        for tab in tabs:
            if tab not in tab_urls:
                print(f"[!] unknown tab: {tab}", file=sys.stderr)
                continue
            results[tab] = capture_tab(page, tab, tab_urls[tab], out_dir)
            if tab == "participants":
                # After the page has rendered, harvest the structured list
                # straight from the DOM — works even if the data never came
                # through a JSON XHR (e.g. inlined in a <script> tag).
                teams = scrape_participants_by_team(page)
                (out_dir / "participants_by_team.json").write_text(
                    json.dumps(teams, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                total = sum(len(t["athletes"]) for t in teams)
                print(
                    f"    scraped {len(teams)} team(s) / {total} athlete(s) "
                    f"from DOM -> {out_dir}/participants_by_team.json",
                    file=sys.stderr,
                )

            if tab == "plannings":
                fights = harvest_planning(page)
                (out_dir / "planning.json").write_text(
                    json.dumps(fights, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                print(
                    f"    scraped {len(fights)} fight slot(s) "
                    f"from DOM -> {out_dir}/planning.json",
                    file=sys.stderr,
                )

            if tab == "brackets":
                # Bracket categories are lazy-mounted *and* unmounted by
                # IntersectionObserver. Scroll + scrape in the same loop,
                # accumulating so nothing is lost when Vue later unmounts.
                cats = harvest_brackets(page, out_dir)
                (out_dir / "brackets_by_category.json").write_text(
                    json.dumps(cats, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                populated = sum(1 for c in cats if c.get("status") == "populated")
                fighters  = sum(len(c.get("fighters", [])) for c in cats)
                print(
                    f"    scraped {len(cats)} categor(ies), "
                    f"{populated} populated, {fighters} fighter slot(s) "
                    f"-> {out_dir}/brackets_by_category.json",
                    file=sys.stderr,
                )

        browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
