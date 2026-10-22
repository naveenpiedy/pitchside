"""Batting hand and bowling style for players (Cricsheet has neither).

Source chain: Cricsheet register (Cricinfo id) -> Wikidata (P2697, ESPNcricinfo
player ID) -> English Wikipedia article -> `batting` / `bowling` fields of the
cricketer infobox. Cricinfo itself blocks automated requests.

Results are merged into data/player_attributes.csv. Rows whose `source` isn't
'wikipedia' (e.g. 'manual') are never overwritten, so hand edits always win.
"""

from __future__ import annotations

import csv
import re
import time
from pathlib import Path
from urllib.parse import unquote

import duckdb
import httpx

from pitchside.config import ATTRIBUTES_CSV, REGISTRY_DB, load_tournaments

USER_AGENT = "pitchside/0.1 (personal cricket analytics; https://github.com/naveenpiedy/pitchside)"
WIKIDATA_SPARQL = "https://query.wikidata.org/sparql"
WIKIPEDIA_API = "https://en.wikipedia.org/w/api.php"
COLUMNS = ["player_id", "name", "batting_hand", "bowling_arm", "bowling_style",
           "bowl_type", "bowl_subtype", "source", "wiki_title"]
SPIN_SUBTYPES = {"OB", "LB", "SLA", "LWS"}


# --- parsing and classification -------------------------------------------

def clean_wikitext(value: str) -> str:
    """Reduce an infobox value to plain text, keeping only the first item of lists."""
    v = re.sub(r"<!--.*?-->", "", value, flags=re.S)
    v = re.sub(r"<ref[^>]*/>|<ref[^>]*>.*?</ref>", "", v, flags=re.S)
    v = re.sub(r"\[\[(?:[^\]|]*\|)?([^\]]*)\]\]", r"\1", v)       # [[a|b]] -> b, [[a]] -> a
    m = re.match(r"\s*\{\{\s*(?:ubl|unbulleted list|plainlist|plain list|hlist|flatlist|bulleted list)\s*\|?(.*)",
                 v, flags=re.S | re.I)
    if m:  # list template: first item
        items = [i.strip(" *\n") for i in re.split(r"\||\n\s*\*", m.group(1))]
        v = next((i for i in items if i and not i.startswith("}}")), "")
    v = re.sub(r"\{\{[^{}]*\}\}", "", v).replace("}}", "").replace("{{", "")
    v = re.sub(r"<br\s*/?>.*", "", v, flags=re.S)
    return re.sub(r"\s+", " ", v.replace("'''", "").replace("''", "")).strip(" ,;")


def infobox_fields(wikitext: str, fields=("batting", "bowling")) -> dict[str, str]:
    out = {}
    for f in fields:
        m = re.search(rf"\n\s*\|\s*{f}\s*=(.*?)(?=\n\s*\||\n\}}\}})", wikitext, flags=re.S)
        if m:
            out[f] = clean_wikitext(m.group(1))
    return out


def classify_batting(text: str | None) -> str | None:
    t = (text or "").lower()
    if "left" in t:
        return "left"
    if "right" in t:
        return "right"
    return None


def classify_bowling(text: str | None) -> tuple[str | None, str | None, str | None]:
    """Return (arm, type, subtype) for a bowling style such as 'Right-arm fast-medium'.

    Subtypes: RF/RFM/RMF/RM (and L*) for pace; OB off-break, LB leg-break,
    SLA left-arm orthodox, LWS left-arm wrist spin.
    """
    t = (text or "").lower().replace("–", "-").replace("—", "-")
    if not t or t in {"none", "-", "n/a", "did not bowl"}:
        return None, None, None
    arm = "left" if "left" in t else "right" if "right" in t else None

    if arm == "left" and re.search(r"chinaman|wrist|unorthodox", t):
        sub = "LWS"
    elif "orthodox" in t:
        sub, arm = "SLA", arm or "left"
    elif re.search(r"leg[ -]?(break|spin)|googly", t):
        sub = "LWS" if arm == "left" else "LB"
    elif re.search(r"off[ -]?(break|spin)", t):
        sub = "OB"
    elif re.search(r"fast|medium|seam|swing", t):
        if re.search(r"fast[ -]medium", t):
            speed = "FM"
        elif re.search(r"medium[ -]fast", t):
            speed = "MF"
        elif "fast" in t:
            speed = "F"
        else:
            speed = "M"
        sub = ("L" if arm == "left" else "R") + speed
    elif "slow" in t or "spin" in t:
        return arm, "spin", None
    else:
        return arm, None, None
    if arm is None and sub in {"OB", "LB"}:  # 'Leg break googly' etc. imply the right arm
        arm = "right"
    return arm, ("spin" if sub in SPIN_SUBTYPES else "pace"), sub


# --- fetching -----------------------------------------------------------------

def _get(client: httpx.Client, url: str, params: dict, retries: int = 3) -> dict:
    for attempt in range(retries):
        r = client.get(url, params=params)
        if r.status_code == 429 or r.status_code >= 500:
            time.sleep(2 ** attempt * 2)
            continue
        r.raise_for_status()
        return r.json()
    r.raise_for_status()
    return {}


def lookup_articles(client: httpx.Client, cricinfo_ids: list[str], batch: int = 200) -> dict[str, str]:
    """Cricinfo id -> English Wikipedia title."""
    found: dict[str, str] = {}
    for i in range(0, len(cricinfo_ids), batch):
        values = " ".join(f'"{c}"' for c in cricinfo_ids[i:i + batch])
        query = f"""SELECT ?ci ?article WHERE {{ VALUES ?ci {{ {values} }} ?item wdt:P2697 ?ci .
                    ?article schema:about ?item ; schema:isPartOf <https://en.wikipedia.org/> . }}"""
        data = _get(client, WIKIDATA_SPARQL, {"query": query, "format": "json"})
        for b in data["results"]["bindings"]:
            found[b["ci"]["value"]] = unquote(b["article"]["value"].rsplit("/", 1)[1]).replace("_", " ")
        time.sleep(1)
    return found


def fetch_infoboxes(client: httpx.Client, titles: list[str], batch: int = 50) -> dict[str, dict[str, str]]:
    """Requested title -> {'batting': ..., 'bowling': ...}."""
    out: dict[str, dict[str, str]] = {}
    for i in range(0, len(titles), batch):
        chunk = titles[i:i + batch]
        data = _get(client, WIKIPEDIA_API, {
            "action": "query", "prop": "revisions", "rvprop": "content", "rvslots": "main",
            "format": "json", "formatversion": 2, "redirects": 1, "titles": "|".join(chunk),
        })["query"]
        # Map the final page title back to the title we asked for.
        back = {t: t for t in chunk}
        for step in ("normalized", "redirects"):
            for m in data.get(step, []):
                back[m["to"]] = back.get(m["from"], m["from"])
        for page in data.get("pages", []):
            revs = page.get("revisions")
            if revs:
                out[back.get(page["title"], page["title"])] = infobox_fields(revs[0]["slots"]["main"]["content"])
        time.sleep(0.5)
    return out


# --- CSV merge ---------------------------------------------------------------

def read_csv(path: Path = ATTRIBUTES_CSV) -> dict[str, dict]:
    if not path.exists():
        return {}
    with open(path, newline="", encoding="utf-8") as f:
        return {r["player_id"]: {c: r.get(c, "") or "" for c in COLUMNS} for r in csv.DictReader(f)}


def write_csv(rows: dict[str, dict], path: Path = ATTRIBUTES_CSV) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        for r in sorted(rows.values(), key=lambda r: r["name"]):
            w.writerow(r)


def loaded_players() -> list[tuple[str, str, str | None]]:
    """(player_id, name, cricinfo id) for every player in any loaded competition."""
    con = duckdb.connect()
    try:
        con.execute(f"ATTACH '{REGISTRY_DB}' AS reg (READ_ONLY)")
        parts = []
        for t in load_tournaments().values():
            if t.db_path.exists():
                con.execute(f"ATTACH '{t.db_path}' AS {t.key} (READ_ONLY)")
                parts.append(f"SELECT player_id, name FROM {t.key}.players")
        if not parts:
            return []
        return con.execute(f"""
            SELECT pl.player_id, any_value(pl.name), any_value(p.key_cricinfo)
            FROM ({' UNION ALL '.join(parts)}) pl
            LEFT JOIN reg.people p ON p.identifier = pl.player_id
            GROUP BY pl.player_id""").fetchall()
    finally:
        con.close()


def fetch_attributes(refresh: bool = False, log=print) -> dict[str, int]:
    rows = read_csv()
    players = loaded_players()
    todo = [(pid, name, ci) for pid, name, ci in players
            if pid not in rows or (refresh and rows[pid]["source"] == "wikipedia")]
    with_ci = [p for p in todo if p[2]]
    log(f"{len(players)} players loaded, {len(todo)} to fetch ({len(with_ci)} with a Cricinfo id)")
    if not todo:
        return {"fetched": 0, "classified": 0, "not_found": 0}

    with httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=60, follow_redirects=True) as client:
        titles = lookup_articles(client, [ci for _, _, ci in with_ci])
        log(f"Wikidata: {len(titles)} Wikipedia articles found")
        boxes = fetch_infoboxes(client, sorted(set(titles.values())))

    stats = {"fetched": 0, "classified": 0, "not_found": 0}
    for pid, name, ci in todo:
        title = titles.get(ci or "")
        box = boxes.get(title, {}) if title else {}
        arm, btype, sub = classify_bowling(box.get("bowling"))
        rows[pid] = {
            "player_id": pid, "name": name,
            "batting_hand": classify_batting(box.get("batting")) or "",
            "bowling_arm": arm or "", "bowling_style": box.get("bowling", ""),
            "bowl_type": btype or "", "bowl_subtype": sub or "",
            "source": "wikipedia", "wiki_title": title or "",
        }
        stats["fetched"] += 1
        stats["classified" if btype else "not_found"] += 1
    write_csv(rows)
    return stats
