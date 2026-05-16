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
                                                   # citation count
  python3 tools/update_citations.py --refresh      # fast refresh: hit only the
                                                   # source recorded as
                                                   # citations_source for each
                                                   # paper (~3x faster than --all)
  python3 tools/update_citations.py --all          # query every source for every
                                                   # paper - slow but thorough
  python3 tools/update_citations.py --dry-run      # show what would change
                                                   # without writing anything

Three citation sources are queried (OpenAlex, Semantic Scholar, Crossref),
each via three lookup methods (recorded DOI, constructed DOI from paper
number, paper-number search with Whitson-author filter). The MAX count is
kept; the specific source that supplied it is stored as `citations_source`
on the YAML, e.g. "OpenAlex (DOI)" or "Semantic Scholar (constructed DOI)".

Citation-count semantics:
  int  > 0 - a real count; use this for display
  int = 0  - all sources returned 0. Treated as "no badge to show" in JS.
  None     - never looked up, or all sources failed
  str      - legacy editorial value; replaced on next run
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


# ---------- OpenAlex ----------

def openalex_lookup_by_doi(doi: str, mailto: str) -> Optional[int]:
    if not is_valid_doi(doi):
        return None
    data = openalex_get(f"https://api.openalex.org/works/doi:{urllib.parse.quote(doi)}", mailto)
    if not data:
        return None
    return data.get("cited_by_count")


def _oa_result_has_whitson(result: dict) -> bool:
    for a in result.get("authorships") or []:
        author = (a.get("author") or {}).get("display_name") or ""
        if "whitson" in author.lower():
            return True
    return False


def openalex_search(query: str, mailto: str) -> Optional[int]:
    data = openalex_get(
        f"https://api.openalex.org/works?search={urllib.parse.quote(query)}&per-page=5",
        mailto,
    )
    if not data:
        return None
    for r in data.get("results") or []:
        if _oa_result_has_whitson(r):
            return r.get("cited_by_count")
    return None


# ---------- Semantic Scholar ----------

def semanticscholar_get(url: str) -> Optional[dict]:
    req = urllib.request.Request(url, headers={"User-Agent": "curtis-career-citation-sweep"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.load(resp)
    except Exception:
        return None


def _ss_result_has_whitson(result: dict) -> bool:
    for a in result.get("authors") or []:
        if "whitson" in (a.get("name") or "").lower():
            return True
    return False


def semanticscholar_lookup_by_doi(doi: str) -> Optional[int]:
    if not is_valid_doi(doi):
        return None
    data = semanticscholar_get(
        f"https://api.semanticscholar.org/graph/v1/paper/DOI:{urllib.parse.quote(doi)}?fields=citationCount,authors"
    )
    if not data:
        return None
    return data.get("citationCount")


def semanticscholar_search(query: str) -> Optional[int]:
    data = semanticscholar_get(
        f"https://api.semanticscholar.org/graph/v1/paper/search?query={urllib.parse.quote(query)}&limit=5&fields=citationCount,authors,title,year"
    )
    if not data:
        return None
    for r in data.get("data") or []:
        if _ss_result_has_whitson(r):
            return r.get("citationCount")
    return None


# ---------- Crossref ----------

def crossref_get(url: str, mailto: str) -> Optional[dict]:
    """Crossref API. Polite-pool via mailto in URL or User-Agent."""
    full = f"{url}{'&' if '?' in url else '?'}mailto={urllib.parse.quote(mailto)}"
    req = urllib.request.Request(full, headers={"User-Agent": f"curtis-career-citation-sweep ({mailto})"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.load(resp)
    except Exception:
        return None


def crossref_lookup_by_doi(doi: str, mailto: str) -> Optional[int]:
    if not is_valid_doi(doi):
        return None
    data = crossref_get(f"https://api.crossref.org/works/{urllib.parse.quote(doi)}", mailto)
    if not data:
        return None
    return (data.get("message") or {}).get("is-referenced-by-count")


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


def is_paper(d: dict) -> bool:
    """
    True if this entry represents a paper / conference contribution / journal
    article that should plausibly have a citation count. False for books,
    monographs, internal notes, industry-magazine summaries, and HOT-forum /
    AAPG / IBC tutorials.

    A paper qualifies if either:
      - it has a recognisable SPE / URTeC / IPTC / EUR / SCA-style paper number
      - OR it has a valid DOI (covers journal articles like 1992 FPE Soreide-
        Whitson, 1993 IECR Riazi-Whitson, 2023 FPE Michelsen recollection,
        etc. that don't carry an SPE conference number)
    """
    pn = (d.get("paper_number") or "").strip()
    if pn and re.match(r"^(SPE|URTeC|IPTC|EUR|SCA|JPSE|FPE)[-\s]?\d", pn, re.I):
        return True
    if is_valid_doi(d.get("doi") or ""):
        return True
    return False


def needs_update(d: dict, mode: str) -> bool:
    citations = d.get("citations")
    if not is_paper(d):
        return False  # skip non-papers entirely
    if mode == "all":
        return True
    return citations is None or citations == "not catalogued" or isinstance(citations, str)


SOURCE_QUERIES_ALL = [
    ("OpenAlex (DOI)", "openalex", "doi"),
    ("OpenAlex (constructed DOI)", "openalex", "constructed_doi"),
    ("OpenAlex (search)", "openalex", "search"),
    ("Semantic Scholar (DOI)", "semanticscholar", "doi"),
    ("Semantic Scholar (constructed DOI)", "semanticscholar", "constructed_doi"),
    ("Semantic Scholar (search)", "semanticscholar", "search"),
    ("Crossref (DOI)", "crossref", "doi"),
    ("Crossref (constructed DOI)", "crossref", "constructed_doi"),
]


def _query_one(source: str, method: str, doi: str, constructed: str, paper_number: str, mailto: str) -> Optional[int]:
    """Run a single (source, method) query and return the citation count or None."""
    target_doi = doi if method == "doi" else (constructed if method == "constructed_doi" else None)
    if method in ("doi", "constructed_doi"):
        if not target_doi:
            return None
        if source == "openalex":
            return openalex_lookup_by_doi(target_doi, mailto)
        if source == "semanticscholar":
            return semanticscholar_lookup_by_doi(target_doi)
        if source == "crossref":
            return crossref_lookup_by_doi(target_doi, mailto)
    if method == "search":
        if not paper_number:
            return None
        if source == "openalex":
            return openalex_search(paper_number, mailto)
        if source == "semanticscholar":
            return semanticscholar_search(paper_number)
    return None


def resolve_citation(paper: dict, mailto: str, only_source: Optional[str] = None) -> tuple[Optional[int], str, list[tuple[int, str]]]:
    """
    Look up citation count and return (best_count, best_label, all_hits).

    If `only_source` is set (a label like "OpenAlex (DOI)"), only that single
    query is executed - the fast-refresh path. Otherwise all eight queries
    run and we take the MAX of whatever returns.
    """
    year = paper.get("year") or (int(paper["id"][:4]) if paper.get("id", "")[:4].isdigit() else None)
    doi = (paper.get("doi") or "").strip()
    paper_number = (paper.get("paper_number") or "").strip()
    constructed = construct_doi(paper_number, year)
    if constructed and constructed.lower() == doi.lower():
        constructed = None

    hits: list[tuple[int, str]] = []
    queries = [q for q in SOURCE_QUERIES_ALL if only_source is None or q[0] == only_source]

    for label, source, method in queries:
        c = _query_one(source, method, doi, constructed or "", paper_number, mailto)
        if c is not None:
            hits.append((c, label))

    if not hits:
        return None, "no hit", []

    best_count = max(c for c, _ in hits)
    winners = [label for c, label in hits if c == best_count]
    # Prefer OpenAlex > Semantic Scholar > Crossref for the recorded best-source
    # tag when several sources tie at the max.
    rank = {"OpenAlex": 0, "Semantic Scholar": 1, "Crossref": 2}
    winners.sort(key=lambda w: rank.get(w.split(" (")[0], 99))
    return best_count, winners[0], hits


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
    grp = ap.add_mutually_exclusive_group()
    grp.add_argument("--all", action="store_true",
                     help="Re-query every source for every paper. Slow, thorough.")
    grp.add_argument("--refresh", action="store_true",
                     help="Fast refresh: for each paper, hit only the source tagged "
                          "as `citations_source` from the previous run. Falls back to "
                          "full multi-source lookup for any paper with no recorded source.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Show what would change without writing anything.")
    ap.add_argument("--mailto", default=DEFAULT_MAILTO,
                    help=f"OpenAlex polite-pool mailto (default {DEFAULT_MAILTO})")
    ap.add_argument("--sleep", type=float, default=0.1,
                    help="Delay between API calls in seconds (default 0.1)")
    ap.add_argument("--limit", type=int, default=None,
                    help="Only process the first N papers needing an update (for testing)")
    args = ap.parse_args()

    mode = "all" if args.all else ("refresh" if args.refresh else "missing-only")
    yamls = sorted(DATA_DIR.glob("*.yaml"))

    targets = []
    skipped_non_papers = 0
    for f in yamls:
        d = load_yaml(f)
        if not is_paper(d):
            skipped_non_papers += 1
            continue
        # In refresh mode, examine every paper (we re-hit each one's recorded
        # best source). In missing-only mode, skip papers that already have a
        # usable int count.
        if mode == "missing-only" and not needs_update(d, mode):
            continue
        targets.append((f, d))

    if args.limit:
        targets = targets[: args.limit]

    print(f"Mode: {mode}{'  (dry-run)' if args.dry_run else ''}")
    print(f"Candidate papers: {len(targets)} / {len(yamls)}  (skipped {skipped_non_papers} non-paper entries)")
    print(f"OpenAlex mailto: {args.mailto}")
    print()

    updated = 0
    no_hit = 0
    for f, d in targets:
        pid = d["id"]
        prev_count = d.get("citations")
        prev_source = d.get("citations_source")

        # In refresh mode, hit only the recorded best source. If no source is
        # recorded yet (first run), fall back to a full multi-source lookup.
        only = None
        if mode == "refresh" and prev_source and prev_source in {q[0] for q in SOURCE_QUERIES_ALL}:
            only = prev_source

        count, source_label, all_hits = resolve_citation(d, args.mailto, only_source=only)
        time.sleep(args.sleep)

        if count is None:
            print(f"  [   no hit] {pid}  (prev={prev_count!r}, source={prev_source!r})")
            no_hit += 1
            continue

        # Build a compact "via" string showing all hits for the diff log.
        hits_str = ", ".join(f"{c}@{lbl}" for c, lbl in sorted(all_hits, key=lambda x: -x[0]))

        if count == prev_count and source_label == prev_source:
            print(f"  [unchanged] {pid}  {count}  best={source_label}  [{hits_str}]")
            continue

        print(f"  [  UPDATED] {pid}  {prev_count!r} -> {count}  best={source_label}  [{hits_str}]")
        if args.dry_run:
            continue

        d["citations"] = count
        d["citations_source"] = source_label
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
