#!/usr/bin/env python3
"""
Refresh OpenAlex citation counts across the corpus.

Looks at every paper_data/*.yaml file. For each paper:
  1. If a DOI is recorded, query OpenAlex for cited_by_count.
  2. If no DOI but a SPE / URTeC / IPTC paper number is recorded, try to
     construct a DOI and look that up.
  3. If still nothing, fall back to OpenAlex search by paper-number string.
The fetched count is written back to:
  - paper_data/<id>.yaml          (the source of truth; also sets
                                   citations_source: "OpenAlex")
  - paper_summaries/whitson_papers.jsonl  (in-place line replacement)
  - index.html JS papers array            (in-place line replacement)

After running, re-render the per-paper summaries with:
  python3 tools/build_summary.py --all

Usage:
  python3 tools/update_citations.py                # update only papers that
                                                   # currently have no usable
                                                   # citation count (the
                                                   # default - the safe option)
  python3 tools/update_citations.py --all          # refresh every paper
                                                   # (heavier on OpenAlex)
  python3 tools/update_citations.py --dry-run      # show what would change
                                                   # without writing anything
  python3 tools/update_citations.py --mailto X     # override the polite-pool
                                                   # email passed to OpenAlex

Citation-count semantics:
  int  > 0 - a real count; use this for display
  int = 0  - OpenAlex returned 0 (or 'not in OpenAlex' after search). Treated
             as "no badge to show" in index.html JS.
  None     - never looked up, or lookup failed
  str      - legacy editorial value; not overwritten unless --all
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "paper_data"
JSONL = PROJECT_ROOT / "paper_summaries" / "whitson_papers.jsonl"
INDEX = PROJECT_ROOT / "index.html"
DEFAULT_MAILTO = "vinomarky@gmail.com"


# ----------------------------------------------------------------------- YAML

sys.path.insert(0, str(PROJECT_ROOT / "tools"))
from extract_summaries import _LiteralStr, _mark_literals, _literal_str_representer  # noqa: E402

yaml.add_representer(_LiteralStr, _literal_str_representer)


def load_yaml(path: Path) -> dict:
    with path.open() as f:
        return yaml.safe_load(f)


def write_yaml(path: Path, data: dict) -> None:
    with path.open("w") as f:
        yaml.dump(_mark_literals(data), f, allow_unicode=True, sort_keys=False, width=1000)


# -------------------------------------------------------------------- OpenAlex


def openalex_get(url: str, mailto: str) -> Optional[dict]:
    """Single GET against OpenAlex with polite-pool mailto."""
    sep = "&" if "?" in url else "?"
    full = f"{url}{sep}mailto={urllib.parse.quote(mailto)}"
    req = urllib.request.Request(full, headers={"User-Agent": f"curtis-career-citation-sweep ({mailto})"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.load(resp)
    except Exception as e:
        print(f"  ! HTTP error on {url[:80]}: {e}", file=sys.stderr)
        return None


def is_valid_doi(s: str) -> bool:
    """Cheap check: a real DOI starts with '10.' and contains a '/'."""
    if not s:
        return False
    s = s.strip()
    return s.startswith("10.") and "/" in s and not any(
        bad in s.lower() for bad in ("none", "not available", "n/a", "internal", "(", " ")
    )


def lookup_by_doi(doi: str, mailto: str) -> Optional[int]:
    if not is_valid_doi(doi):
        return None
    data = openalex_get(f"https://api.openalex.org/works/doi:{urllib.parse.quote(doi)}", mailto)
    if not data:
        return None
    return data.get("cited_by_count")


def _result_has_whitson(result: dict) -> bool:
    """Verify an OpenAlex search hit by requiring 'Whitson' in its author list."""
    for a in result.get("authorships") or []:
        author = (a.get("author") or {}).get("display_name") or ""
        if "whitson" in author.lower():
            return True
    return False


def search_for(query: str, mailto: str, require_whitson: bool = True) -> Optional[int]:
    """Full-text search; returns the count for the FIRST hit that includes Whitson as author."""
    data = openalex_get(
        f"https://api.openalex.org/works?search={urllib.parse.quote(query)}&per-page=5",
        mailto,
    )
    if not data:
        return None
    for r in data.get("results") or []:
        if not require_whitson or _result_has_whitson(r):
            return r.get("cited_by_count")
    return None


def construct_doi(paper_number: str, year: int) -> Optional[str]:
    """
    Try to synthesise a DOI from a paper number.

    SPE conference paper:   SPE-12233-MS    -> 10.2118/12233-MS
    SPE journal:            SPE-12233-PA    -> 10.2118/12233-PA
    URTeC:                  URTeC-539       -> 10.15530/urtec-{year}-539
    IPTC:                   IPTC-19596-MS   -> 10.2523/IPTC-19596-MS
    EUR:                    EUR-183         -> 10.2118/EUR-183
    """
    if not paper_number:
        return None
    pn = paper_number.strip()
    m = re.match(r"^SPE-(\d+)(?:-(MS|PA))?$", pn, re.I)
    if m:
        suffix = (m.group(2) or "MS").upper()
        return f"10.2118/{m.group(1)}-{suffix}"
    m = re.match(r"^URTeC-(\d+)$", pn, re.I)
    if m and year:
        return f"10.15530/urtec-{year}-{m.group(1)}"
    m = re.match(r"^IPTC-(\d+)(?:-MS)?$", pn, re.I)
    if m:
        return f"10.2523/IPTC-{m.group(1)}-MS"
    return None


def needs_update(citations, mode: str) -> bool:
    if mode == "all":
        return True
    # default: --missing-only
    return citations is None or citations == "not catalogued" or isinstance(citations, str)


def resolve_citation(paper: dict, mailto: str) -> tuple[Optional[int], str]:
    """
    Try a DOI lookup, then a constructed-DOI lookup, then a paper-number search.
    Returns (count, source_label) - count may be None if all attempts failed.
    """
    year = paper.get("year") or (int(paper["id"][:4]) if paper.get("id", "")[:4].isdigit() else None)

    doi = (paper.get("doi") or "").strip()
    paper_number = (paper.get("paper_number") or "").strip()

    if doi:
        c = lookup_by_doi(doi, mailto)
        if c is not None:
            return c, f"DOI lookup ({doi})"

    constructed = construct_doi(paper_number, year)
    if constructed and constructed.lower() != doi.lower():
        c = lookup_by_doi(constructed, mailto)
        if c is not None:
            return c, f"constructed DOI ({constructed})"

    if paper_number:
        c = search_for(paper_number, mailto)
        if c is not None:
            return c, f"paper-number search ({paper_number})"

    return None, "no hit"


# ------------------------------------------------------------------- in-place file patchers


def patch_jsonl(paper_id: str, paper_number: str, new_count: int) -> bool:
    """Replace the citations field for the matching line in the JSONL."""
    lines = JSONL.read_text().splitlines()
    changed = False
    for i, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if d.get("id") == paper_id:
            d["citations"] = new_count
            d["citations_source"] = "OpenAlex"
            lines[i] = json.dumps(d, ensure_ascii=False)
            changed = True
            break
    if changed:
        JSONL.write_text("\n".join(lines) + "\n")
    return changed


def patch_index_html(paper_number: str, new_count: int) -> bool:
    """
    Replace the citations field in the index.html JS papers array entry that
    has this paper_number. Targets the literal pattern:
        "paper_number": "X", "citations": <something>,
    """
    if not paper_number:
        return False
    content = INDEX.read_text()
    # Match the entry by paper_number and replace just the citations value
    pattern = re.compile(
        r'("paper_number":\s*"' + re.escape(paper_number) + r'",\s*"citations":\s*)(null|"[^"]*"|\d+)'
    )
    new_content, n = pattern.subn(lambda m: f'{m.group(1)}{new_count}', content)
    if n:
        INDEX.write_text(new_content)
        return True
    return False


# ------------------------------------------------------------------- driver


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--all", action="store_true",
                    help="Refresh every paper, not just those missing a count.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Show what would change without writing anything.")
    ap.add_argument("--mailto", default=DEFAULT_MAILTO,
                    help=f"OpenAlex polite-pool mailto (default {DEFAULT_MAILTO})")
    ap.add_argument("--sleep", type=float, default=0.1,
                    help="Delay between API calls in seconds (default 0.1)")
    ap.add_argument("--limit", type=int, default=None,
                    help="Only process the first N papers needing an update (for testing)")
    args = ap.parse_args()

    mode = "all" if args.all else "missing-only"
    yamls = sorted(DATA_DIR.glob("*.yaml"))

    targets = []
    for f in yamls:
        d = load_yaml(f)
        if needs_update(d.get("citations"), mode):
            targets.append((f, d))

    if args.limit:
        targets = targets[: args.limit]

    print(f"Mode: {mode}{'  (dry-run)' if args.dry_run else ''}")
    print(f"Candidate papers: {len(targets)} / {len(yamls)}")
    print(f"OpenAlex mailto: {args.mailto}")
    print()

    updated = 0
    no_hit = 0
    for f, d in targets:
        pid = d["id"]
        prev = d.get("citations")
        count, source = resolve_citation(d, args.mailto)
        time.sleep(args.sleep)

        if count is None:
            print(f"  [   no hit] {pid}  (prev={prev!r})")
            no_hit += 1
            continue

        if count == prev:
            print(f"  [unchanged] {pid}  {count}  via {source}")
            continue

        print(f"  [  UPDATED] {pid}  {prev!r} -> {count}  via {source}")
        if args.dry_run:
            continue

        # Persist
        d["citations"] = count
        d["citations_source"] = "OpenAlex"
        write_yaml(f, d)
        patch_jsonl(pid, d.get("paper_number") or "", count)
        if d.get("paper_number"):
            patch_index_html(d["paper_number"], count)
        updated += 1

    print()
    print(f"Summary: updated={updated}  no-hit={no_hit}  examined={len(targets)}")
    if updated and not args.dry_run:
        print()
        print("Next step: rebuild per-paper HTMLs so the meta-box citation count refreshes:")
        print("  python3 tools/build_summary.py --all")
        print()
        print("Then commit and push.")


if __name__ == "__main__":
    main()
