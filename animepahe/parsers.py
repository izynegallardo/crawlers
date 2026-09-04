"""
    Pure HTML -> dict parsing for animepahe pages.

    No browser/pydoll code lives here on purpose: every function takes a raw
    HTML string (or a BeautifulSoup node) and returns plain data. That makes
    these testable without a browser and keeps scraper.py free of selector
    details. No file I/O either - image downloading lives in scraper.py.

    We use BeautifulSoup here instead of pydoll's ExtractionModel because the
    sidebar info block (.anime-info) is a flat list of <p> tags with the
    label baked into the text ("<strong>Episodes:</strong> 25") rather than
    one CSS target per field. A fixed selector-per-field model can't express
    "find the <p> whose label is Episodes" - so we parse by label text
    instead.
"""

import json
import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from .config import BASE_URL

_INFO_FIELDS = [
    "synonyms",
    "title_japanese",
    "title_spanish",
    "title_french",
    "type",
    "episodes",
    "status",
    "duration",
    "aired_from",
    "aired_to",
    "season",
    "studios",
    "themes",
    "demographics",
    "genres",
    "external_links",
]

# The trailer is only injected into a Magnific Popup iframe after the
# .youtube-preview anchor is clicked - confirmed via devtools, it is not
# in the static page source. So parsing here can't produce it; scraper.py
# clicks the anchor and passes the iframe's embed src through this to
# normalize it into a plain watch URL.
_EMBED_ID_RE = re.compile(r"/embed/([\w-]{11})")


def normalize_youtube_url(embed_src: str | None) -> str | None:
    if not embed_src:
        return None
    match = _EMBED_ID_RE.search(embed_src)
    if not match:
        return None
    return f"https://www.youtube.com/watch?v={match.group(1)}"


def _normalize_external_url(href: str) -> str:
    """External-link hrefs are protocol-relative ("//anilist.co/...") -
    add the scheme back so they're usable as-is."""
    if href.startswith("//"):
        return "https:" + href
    return href


def parse_index_page(html: str) -> dict[str, list[dict]]:
    """
        Return {letter_id: [{pahe_id, title, url}, ...]} for every tab-pane
        found on the index page (id="hash", "A", "B", ... "Z").

        Selector is intentionally just `div.tab-pane[id]` - not scoped under
        `div.index > div.tab-content >` - because we've never confirmed that
        exact ancestor chain against live DOM, only inferred it. Matching
        any tab-pane with an id is safer and costs nothing (an unrelated
        tab-pane elsewhere would just add an unused dict key).

        Entries are merged per letter_id rather than overwritten. If the
        same id shows up more than once in the document (duplicate desktop
        /mobile markup, or a pane element that gets re-rendered), a later
        empty or partial match must never wipe out real entries found
        earlier - only add to them, deduped by pahe_id.
    """
    soup = BeautifulSoup(html, "html.parser")
    letters: dict[str, list[dict]] = {}

    for pane in soup.select("div.tab-pane[id]"):
        letter_id = pane.get("id")
        if not letter_id:
            continue

        parsed = _parse_index_entries(pane)
        existing = letters.get(letter_id)

        if existing is None:
            letters[letter_id] = parsed
        elif parsed:
            seen_ids = {e["pahe_id"] for e in existing}
            existing.extend(e for e in parsed if e["pahe_id"] not in seen_ids)

    return letters


def _parse_index_entries(pane) -> list[dict]:
    entries = []
    for a in pane.select("a[href^='/anime/']"):
        href = a.get("href", "")
        pahe_id = href.rstrip("/").split("/")[-1]
        if not pahe_id:
            continue
        entries.append(
            {
                "pahe_id": pahe_id,
                "title": a.get("title") or a.get_text(strip=True),
                "url": urljoin(BASE_URL, href),
            }
        )
    return entries


def parse_anime_page(html: str, pahe_id: str) -> dict:
    """
        Parse the Summary + Relations + Recommendations tabs. All three
        already live in the page HTML (they're just CSS-toggled), so one
        page load is enough - no tab clicking required.
    """
    soup = BeautifulSoup(html, "html.parser")

    header = soup.select_one("header.anime-header")
    title = _text(header.select_one("h1 span")) if header else None
    title_romaji = _text(header.select_one("h2.japanese")) if header else None

    poster_img = soup.select_one(".anime-poster img")
    image_url = poster_img.get("data-src") if poster_img else None

    synopsis_el = soup.select_one(".anime-synopsis")
    summary = _clean_synopsis(synopsis_el) if synopsis_el else None

    info = _parse_info_sidebar(soup.select_one(".anime-info"))
    info["airing"] = info.get("aired_to") is None

    row = {
        "pahe_id": pahe_id,
        "title": title,
        "title_romaji": title_romaji,
        "summary": summary,
        "relations": json.dumps(
            _parse_relations(soup.select_one(".anime-relation")), ensure_ascii=False
        ),
        "recommendations": json.dumps(
            _parse_card_list(soup.select_one(".anime-recommendation")),
            ensure_ascii=False,
        ),
        "image_url": image_url,
        # Filled in by scraper.py after clicking the trailer preview -
        # not derivable from static HTML, see normalize_youtube_url above.
        "youtube_url": None,
    }
    row.update(info)
    return row


def _text(el) -> str | None:
    return el.get_text(strip=True) if el else None


def _clean_synopsis(el) -> str:
    text = el.get_text(separator="\n", strip=True)
    return re.sub(r"\n{2,}", "\n\n", text).strip()


def _label_of(p) -> str | None:
    strong = p.find("strong")
    label_text = strong.get_text(strip=True) if strong else p.get_text(strip=True)
    if ":" not in label_text:
        return None
    return label_text.split(":", 1)[0].strip().lower()


def _parse_info_sidebar(container) -> dict:
    """
        studios/themes/demographics/genres come back as JSON-encoded lists
        (same convention as relations/recommendations - CSV cells are
        strings, so a real list needs to be encoded to survive round-
        tripping through csv.DictWriter/DictReader). external_links comes
        back as a JSON-encoded list of {name, url}, since each link's
        target is real, useful data, not just a label.
    """
    result = {field: None for field in _INFO_FIELDS}
    if not container:
        return result

    for p in container.find_all("p", recursive=False):
        label = _label_of(p)
        if label is None:
            continue

        text = p.get_text(" ", strip=True)
        value = text.split(":", 1)[1].strip() if ":" in text else text
        link = p.find("a")
        link_texts = [a.get_text(strip=True) for a in p.find_all("a")]

        if label == "synonyms":
            result["synonyms"] = value
        elif label == "japanese":
            result["title_japanese"] = value
        elif label == "spanish":
            result["title_spanish"] = value
        elif label == "french":
            result["title_french"] = value
        elif label == "type":
            result["type"] = link.get_text(strip=True) if link else value
        elif label == "episodes":
            result["episodes"] = value
        elif label == "status":
            result["status"] = link.get_text(strip=True) if link else value
        elif label == "duration":
            result["duration"] = value
        elif label == "aired":
            parts = [t.strip() for t in text.replace("Aired:", "", 1).split(" to ")]
            result["aired_from"] = parts[0] if parts and parts[0] else None
            result["aired_to"] = parts[1] if len(parts) > 1 else None
        elif label == "season":
            result["season"] = link.get_text(strip=True) if link else value
        elif label == "studios":
            # Seen as plain comma-separated text with no <a> tags, but if
            # animepahe ever links studio names (like it does themes), use
            # those instead of guessing at comma placement.
            studios = link_texts if link_texts else [s.strip() for s in value.split(",") if s.strip()]
            result["studios"] = json.dumps(studios, ensure_ascii=False)
        elif label == "themes":
            result["themes"] = json.dumps(link_texts, ensure_ascii=False)
        elif label == "demographic":
            result["demographics"] = json.dumps(link_texts, ensure_ascii=False)
        elif label == "external links":
            links = [
                {"name": a.get_text(strip=True), "url": _normalize_external_url(a.get("href", ""))}
                for a in p.find_all("a")
            ]
            result["external_links"] = json.dumps(links, ensure_ascii=False)

    genre_ul = container.select_one(".anime-genre ul")
    if genre_ul:
        genres = [a.get_text(strip=True) for a in genre_ul.find_all("a")]
        result["genres"] = json.dumps(genres, ensure_ascii=False)

    return result


# Fields backfilled by the column-patch tool (tools not shown here don't
# need to know parsing details - this keeps that scope explicit and in
# one place).
PATCH_FIELDS = ["studios", "themes", "genres", "demographics", "external_links"]


def parse_patch_fields(html: str) -> dict:
    """
        Lightweight parse for the column-patch migration: extracts only
        PATCH_FIELDS from the sidebar, skipping summary/relations/
        recommendations entirely. Used to backfill rows scraped before
        those fields became list/object-array data, without re-parsing (or
        risking accidentally overwriting) anything else in the row.
    """
    soup = BeautifulSoup(html, "html.parser")
    info = _parse_info_sidebar(soup.select_one(".anime-info"))
    return {field: info[field] for field in PATCH_FIELDS}


def _parse_card(card) -> dict | None:
    title_a = card.select_one(".col-9 h5 a")
    if not title_a:
        return None

    href = title_a.get("href", "")
    return {
        "pahe_id": href.rstrip("/").split("/")[-1],
        "title": title_a.get_text(strip=True),
    }


def _parse_card_list(container) -> list[dict]:
    """
        Flat list of {pahe_id, title} cards - used for recommendations,
        which has no heading groups to derive a type from.
    """
    if not container:
        return []

    items = []
    for card in container.select("div.mb-3"):
        data = _parse_card(card)
        if data:
            items.append(data)

    return items


def _parse_relations(container) -> list[dict]:
    """
        Same cards as _parse_card_list, but grouped under headings
        (Sequel, Side Story, Spin-off, Summary...) so each item also carries
        a relation_type - kept because, unlike format/season/poster, it
        isn't duplicated by the related anime's own row.
    """
    if not container:
        return []

    items = []
    for group in container.select(":scope > div"):
        heading = group.select_one("h4 span")
        relation_type = heading.get_text(strip=True) if heading else None

        for card in group.select("div.mb-3"):
            data = _parse_card(card)
            if data:
                data["relation_type"] = relation_type
                items.append(data)

    return items
