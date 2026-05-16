# Curtis Career

A career-arc tribute page for **Professor Curtis Hays Whitson** (NTNU, Trondheim; founder of Whitson AS).

The page surveys fifty years of published work on petroleum-reservoir phase behaviour, equations of state, gas condensate and black-oil PVT, compositional reservoir simulation, liquid-rich-shale workflows, numerical rate-transient analysis, and (most recently) hydrogen-storage fluid modelling. 103 papers are linked, each with a structured technical summary; the timeline, narrative, and collaborator cards trace the connections.

The live page is at **https://mwburgoyne.github.io/curtis-career/**.

## Layout

```
index.html                          The page itself.
assets/                             Portrait and logos.
paper_summaries/
  *.html                            One technical summary per linked paper.
  whitson_papers.jsonl              The same content as a RAG-ready dataset.
paper_data/
  *.yaml                            Per-paper data files - the source of truth
                                    for the summaries. Edit a YAML, re-render.
tools/
  build_summary.py                  YAML -> HTML renderer (Jinja2).
  extract_summaries.py              HTML -> YAML extractor (one-time, kept for re-imports).
  templates/summary.html.j2         The summary template.
```

## Rebuilding the summaries

```bash
# One paper:
python3 tools/build_summary.py paper_data/<id>.yaml

# All papers (e.g. after a template / CSS change):
python3 tools/build_summary.py --all
```

Requires Python 3 with `jinja2` and `pyyaml`.

## Status

Built externally before Curtis's review. `<!-- CURTIS REVIEW -->` HTML comments in `index.html` and a handful of per-paper summaries flag spots that would benefit from his input on dates, citations, and personal context. Pull requests and corrections from the petroleum-engineering community are welcome via GitHub Issues.

## Acknowledgements

The summaries are written by Mark Burgoyne. The underlying technical content is Curtis's own across his published corpus.
