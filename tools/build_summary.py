#!/usr/bin/env python3
"""
Render a paper-summary HTML from its YAML data file.

Usage:
    python build_summary.py paper_data/<id>.yaml [--out paper_summaries/]
    python build_summary.py --all       # rebuilds every YAML under paper_data/
"""
from pathlib import Path
import argparse
import sys
import yaml
from jinja2 import Environment, FileSystemLoader

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_DIR = PROJECT_ROOT / "tools" / "templates"
DATA_DIR = PROJECT_ROOT / "paper_data"
OUT_DIR = PROJECT_ROOT / "paper_summaries"


def authors_html(authors, whitson_index=None):
    """Render authors as a single comma-separated string with <mark> around Whitson."""
    if whitson_index is None:
        whitson_index = next(
            (i for i, a in enumerate(authors) if "Whitson" in a), None
        )
    rendered = []
    for i, a in enumerate(authors):
        if i == whitson_index:
            rendered.append(f"<mark>{a}</mark>")
        else:
            rendered.append(a)
    return ", ".join(rendered)


def render(data_path: Path) -> str:
    with data_path.open() as f:
        data = yaml.safe_load(f)

    env = Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        keep_trailing_newline=True,
    )
    template = env.get_template("summary.html.j2")

    ctx = dict(data)
    ctx["authors_html"] = authors_html(data["authors"], data.get("whitson_index"))
    ctx.setdefault("page_title", f"{data['authors'][0].split()[-1]} et al. - {data['title']}")
    return template.render(**ctx)


def output_path_for(data_path: Path) -> Path:
    return OUT_DIR / f"{data_path.stem}.html"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("yaml", nargs="?")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--out", default=str(OUT_DIR))
    args = ap.parse_args()

    if args.all:
        files = sorted(DATA_DIR.glob("*.yaml"))
        if not files:
            print(f"No YAML files in {DATA_DIR}", file=sys.stderr)
            sys.exit(1)
    elif args.yaml:
        files = [Path(args.yaml)]
    else:
        ap.print_help()
        sys.exit(1)

    for f in files:
        html = render(f)
        out = Path(args.out) / f"{f.stem}.html"
        out.write_text(html)
        print(f"wrote {out}")


if __name__ == "__main__":
    main()
