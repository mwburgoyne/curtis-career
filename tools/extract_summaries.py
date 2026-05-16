#!/usr/bin/env python3
"""
Parse existing per-paper HTML summaries into YAML data files.

The HTML structure across the 97 existing summaries is consistent enough:
- one <h1> = title
- one .meta div with metadata <p> rows
- a series of <h2> headings, each followed by free-form body HTML up to the
  next <h2> or the trailing back-link <a>
- some bodies are wrapped in .influence / .connections / .landmark boxes

We extract each section as raw inner HTML (preserved verbatim), capture
metadata fields by regex on the meta-box paragraphs, and emit one
paper_data/<id>.yaml per input.

Usage:
    python extract_summaries.py                # all paper_summaries/*.html
    python extract_summaries.py <file.html>    # one file
"""
from pathlib import Path
import argparse
import re
import sys
import yaml
from bs4 import BeautifulSoup, NavigableString

PROJECT_ROOT = Path(__file__).resolve().parent.parent
INPUT_DIR = PROJECT_ROOT / "paper_summaries"
OUTPUT_DIR = PROJECT_ROOT / "paper_data"


def _inner_html(tag):
    """Return the inner HTML of a BS4 tag, preserving whitespace."""
    return "".join(str(c) for c in tag.contents).strip()


def _parse_meta(meta_div):
    """Pull labelled fields from the meta div paragraphs."""
    out = {
        "authors": None,
        "authors_affiliation": None,
        "venue": None,
        "paper_number": None,
        "doi": None,
        "funding": None,
        "citations": None,
        "citations_source": None,
        "tags": [],
        "whitson_index": None,
        "extra_meta": [],  # list of {label, value} for any unrecognised labels
    }
    for p in meta_div.find_all("p"):
        text = p.get_text(" ", strip=True)
        if not text:
            continue
        # Strong label?
        strong = p.find("strong")
        # Preserve the original-case label text BEFORE we lowercase it for routing
        original_label_text = strong.get_text(strip=True).rstrip(":") if strong else None
        # Preserve the value as HTML (after the <strong>) so <em>, <sub>, links survive round-trip
        value_html = None
        if strong is not None:
            value_html = re.sub(
                r"^\s*<strong>[^<]*</strong>\s*:?\s*",
                "",
                _inner_html(p),
            ).strip()
        if strong is None:
            # Likely the tag row
            for tag in p.find_all(class_="tag"):
                out["tags"].append(_inner_html(tag))
            # Older HTMLs sometimes also have a <span class="landmark">FOUNDATION PAPER</span> in this row
            for span in p.find_all(class_="landmark"):
                out["landmark_inline_text"] = _inner_html(span)
            continue
        label = strong.get_text(strip=True).rstrip(":").lower()
        # value is everything after the <strong>
        value = text[len(strong.get_text(strip=True)):].lstrip(" :").strip()
        if label in ("authors", "author"):
            authors, w_idx, affil = _parse_authors(p)
            out["authors"] = authors
            out["whitson_index"] = w_idx
            out["authors_affiliation"] = affil
        elif label in (
            "venue", "journal", "conference", "source", "where published",
            "published in", "publisher", "type", "in", "book", "publication",
            "sources", "sources (paired)",
        ):
            # Preserve any inline <em>, <a>, <sub>, etc. via the HTML form
            out["venue"] = value_html if value_html else value
        elif label == "paper number":
            out["paper_number"] = value
        elif label == "doi":
            out["doi"] = value
        elif label == "funding":
            out["funding"] = value
        elif label.startswith("citations") or label.startswith("citation"):
            # Use the ORIGINAL-CASE label text to preserve "OpenAlex" / "Google Scholar" capitalisation
            src_match = re.match(r"Citations?\s*\(([^)]+)\)", original_label_text or "")
            if src_match:
                out["citations_source"] = src_match.group(1)
            cv = value.strip()
            # Try to pull a leading integer or ~integer
            cm = re.match(r"~?(\d+)", cv)
            if cm and cv == cm.group(0):
                out["citations"] = int(cm.group(1))
            elif cm:
                # e.g. "~446 (OpenAlex), the most cited paper of Whitson's career"
                # Capture the integer count and the parenthesised source if present
                out["citations"] = int(cm.group(1))
                src = re.search(r"\(([^)]+)\)", cv)
                if src and not out["citations_source"]:
                    out["citations_source"] = src.group(1)
                # If there's editorial trailing text, stash it
                extra = re.sub(r"^~?\d+\s*(?:\([^)]+\))?\s*,?\s*", "", cv).strip()
                if extra:
                    out["citations_note"] = extra
            elif cv.lower() in ("not catalogued", "n/a", "—", "-", ""):
                out["citations"] = None
            else:
                out["citations"] = cv
        else:
            # Unrecognised label - preserve verbatim so we don't lose content (ISBN, Pages, Year, Format, etc.)
            value_html = re.sub(
                r"^\s*<strong>[^<]*</strong>\s*:?\s*",
                "",
                _inner_html(p),
            )
            out["extra_meta"].append({
                "label": original_label_text,
                "value_html": value_html.strip(),
            })
    # Drop empty optional keys
    if not out.get("citations_source"):
        out.pop("citations_source", None)
    if not out.get("extra_meta"):
        out.pop("extra_meta", None)
    return out


def _parse_authors(p):
    """Authors paragraph may contain <mark>Curtis...</mark> and trailing affiliation in parentheses."""
    raw = _inner_html(p)
    # Drop the leading <strong>Authors:</strong> piece
    raw = re.sub(r"^\s*<strong>[^<]*</strong>\s*:?\s*", "", raw)
    # Split off trailing (...) affiliation if present
    affil = None
    m = re.search(r"\(([^()]+)\)\s*$", raw)
    if m:
        affil = m.group(1).strip()
        raw = raw[: m.start()].strip()
    # Now split on commas at top level (no nested tags expected to span commas)
    # The <mark>...</mark> may surround a single name including comma-free content.
    parts = []
    # Use a soup to walk siblings:
    soup = BeautifulSoup(raw, "html.parser")
    # Build a flat string with comma-separated names, preserving <mark>
    text_with_markers = ""
    for el in soup.contents:
        if isinstance(el, NavigableString):
            text_with_markers += str(el)
        else:
            text_with_markers += str(el)
    # Now split by commas not inside tags. Also handle "X and Y" / "X, Y, and Z" patterns.
    # Strategy: replace <mark>X</mark> with __MK_open__X__MK_close__
    text = re.sub(r"<mark[^>]*>", "\x00", text_with_markers)
    text = re.sub(r"</mark>", "\x01", text)
    # Replace ", and" and trailing " and " with comma (turn Oxford and non-Oxford into uniform comma list)
    text = re.sub(r",\s+and\s+", ", ", text)
    text = re.sub(r"\s+and\s+", ", ", text)
    raw_names = [s.strip() for s in text.split(",")]
    whitson_idx = None
    cleaned = []
    for i, name in enumerate(raw_names):
        if "\x00" in name:
            whitson_idx = i
            name = name.replace("\x00", "").replace("\x01", "")
        cleaned.append(name.strip())
    return cleaned, whitson_idx, affil


def _extract_sections(body, meta_div):
    """Walk siblings after the meta div, grouping under each <h2>."""
    sections = []
    landmark_html = None
    cur = None
    # Iterate ALL elements after the meta div in document order
    nodes = list(meta_div.parent.find_all(recursive=False))
    # Easier: iterate next_siblings on meta_div
    siblings = []
    for s in meta_div.next_siblings:
        if isinstance(s, NavigableString):
            if s.strip():
                siblings.append(s)
            continue
        siblings.append(s)

    cur_heading = None
    cur_box_style = None
    cur_chunks = []

    def flush():
        nonlocal cur_chunks, cur_heading, cur_box_style
        if cur_heading is not None:
            body_html = "".join(cur_chunks).strip()
            # If body content begins with <div class="..."> and ends with </div>, unwrap
            # but only if the wrapper was actually a box class
            if cur_box_style is None:
                m = re.match(
                    r'^<div\s+class="(influence|connections|landmark)">\s*(.+?)\s*</div>\s*$',
                    body_html,
                    flags=re.S,
                )
                if m:
                    cur_box_style = m.group(1)
                    body_html = m.group(2).strip()
            section = {"heading": cur_heading, "body_html": body_html}
            if cur_box_style:
                section["box_style"] = cur_box_style
            sections.append(section)
        cur_chunks = []
        cur_heading = None
        cur_box_style = None

    for s in siblings:
        name = getattr(s, "name", None)
        # Skip trailing back-link anchor
        if name == "a" and "back-link" in (s.get("class") or []):
            continue
        # Standalone landmark box BEFORE first h2 - capture as landmark_html
        if name == "div" and any(c in ("landmark", "landmark-box") for c in (s.get("class") or [])) and cur_heading is None:
            landmark_html = _inner_html(s)
            continue
        if name == "h2":
            flush()
            cur_heading = s.get_text(strip=True)
            continue
        if cur_heading is None:
            # Skip stray content (e.g. extra blank text nodes) before first h2
            if isinstance(s, NavigableString):
                continue
            # Capture into a synthetic "Preamble" if there is real content before h2
            continue
        if isinstance(s, NavigableString):
            cur_chunks.append(str(s))
        else:
            cur_chunks.append(str(s))
    flush()
    return sections, landmark_html


def extract(html_path: Path) -> dict:
    soup = BeautifulSoup(html_path.read_text(), "html.parser")
    # Preserve inline tags (<sub>, <sup>, <em>) inside titles via inner HTML
    title = _inner_html(soup.find("h1")).strip()
    meta_div = soup.find("div", class_="meta")
    meta = _parse_meta(meta_div)
    sections, landmark_html = _extract_sections(soup.body, meta_div)

    head_title = soup.find("title")
    page_title = head_title.get_text(strip=True) if head_title else None

    data = {
        "id": html_path.stem,
        "title": title,
        "page_title": page_title,
        **meta,
        "landmark": landmark_html is not None,
    }
    if landmark_html:
        data["landmark_html"] = landmark_html
    data["sections"] = sections
    # Drop None-valued optional keys for tidiness
    for k in list(data.keys()):
        if data[k] is None:
            data[k] = None  # keep — useful for round-trip stability
    return data


class _LiteralStr(str):
    pass


def _literal_str_representer(dumper, data):
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")


yaml.add_representer(_LiteralStr, _literal_str_representer)


def _mark_literals(obj):
    """Promote long multi-line strings to YAML literal-block scalars for readability."""
    if isinstance(obj, dict):
        return {k: _mark_literals(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_mark_literals(v) for v in obj]
    if isinstance(obj, str) and ("\n" in obj or len(obj) > 120):
        return _LiteralStr(obj)
    return obj


def write_yaml(data: dict, out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    marked = _mark_literals(data)
    with out_path.open("w") as f:
        yaml.dump(
            marked, f, allow_unicode=True, sort_keys=False, width=1000
        )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("html", nargs="?")
    args = ap.parse_args()

    if args.html:
        files = [Path(args.html)]
    else:
        files = sorted(INPUT_DIR.glob("*.html"))

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    for f in files:
        data = extract(f)
        out = OUTPUT_DIR / f"{f.stem}.yaml"
        write_yaml(data, out)
        print(f"wrote {out}")


if __name__ == "__main__":
    main()
