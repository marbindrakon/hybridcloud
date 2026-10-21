#!/usr/bin/env python3
"""Check the relative links (and their #anchors) of the documentation front door.

Scope: the README, docs/, samples/README.md and the architecture entry pages; more files can be
given as arguments. Links inside fenced code are ignored; http(s) and mailto links are not
fetched.

usage: check-doc-links.py [FILE ...]     exit 1 when a link points nowhere
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCOPE = ["README.md", "bootstrap/README.md", "docs/**/*.md", "samples/README.md",
         "architecture/README.md",
         "architecture/docs/architecture.md"]
LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")


def slug(heading):
    s = heading.strip().lower()
    s = re.sub(r"[^\w\- ]", "", s)
    return s.replace(" ", "-")


def anchors(path):
    out = set()
    fence = False
    for ln in path.read_text(encoding="utf-8").splitlines():
        if ln.strip().startswith("```"):
            fence = not fence
        elif not fence and ln.startswith("#"):
            out.add(slug(ln.lstrip("#")))
    return out


def files(args):
    if args:
        return [ROOT / a for a in args]
    seen = []
    for g in SCOPE:
        for p in sorted(ROOT.glob(g)):
            if p.is_file() and p not in seen:
                seen.append(p)
    return seen


def main():
    bad = checked = 0
    for path in files(sys.argv[1:]):
        fence = False
        for n, ln in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if ln.strip().startswith("```"):
                fence = not fence
                continue
            if fence:
                continue
            for target in LINK.findall(re.sub(r"`[^`]*`", "", ln)):
                if re.match(r"^[a-z]+:", target):
                    continue
                checked += 1
                ref, _, frag = target.partition("#")
                dest = (path.parent / ref).resolve() if ref else path
                if not dest.exists():
                    bad += 1
                    print(f"{path.relative_to(ROOT)}:{n}: broken link {target}")
                elif frag and dest.suffix == ".md" and slug(frag) not in anchors(dest):
                    bad += 1
                    print(f"{path.relative_to(ROOT)}:{n}: no heading #{frag} in {ref or 'this page'}")
    print(f"check-doc-links: {checked} links, {bad} broken")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
