#!/usr/bin/env python3
"""Write the reasons table of docs/tenant/status-reasons.md from the reasons registry.

The registry (one entry per condition reason: who acts, its class, a tenant-safe summary) is
eda/rulebooks/collections/ansible_collections/hybridsovereign/shc/plugins/module_utils/
shc_status_reasons.py. The table lists every reason that is not a plain "ready" reason, between
<!-- reasons:begin --> and <!-- reasons:end -->, grouped by what the object is doing.

usage:
  python3 -I scripts/gen-reason-table.py           write the table
  python3 -I scripts/gen-reason-table.py --check   exit 1 when the table is out of date
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "eda/rulebooks/collections/ansible_collections/hybridsovereign/shc/plugins/"
          "module_utils/shc_status_reasons.py")
GUIDE = ROOT / "docs/tenant/status-reasons.md"
BEGIN, END = "<!-- reasons:begin -->", "<!-- reasons:end -->"
CLASSES = [("progress", "Working on it"), ("waiting", "Waiting"), ("blocked", "Blocked"),
           ("failed", "Failed")]
ACTORS = {"A": "Nobody: it continues by itself", "T": "You (the tenant)",
          "P": "The platform team"}


def load():
    # The registry is pure data without imports (its header says so); run it in an empty namespace.
    ns = {"__name__": "shc_status_reasons"}
    exec(compile(SOURCE.read_text(encoding="utf-8"), str(SOURCE), "exec"), ns)  # noqa: S102
    return ns["REASONS"]


def table():
    reasons = load()
    lines = []
    for klass, title in CLASSES:
        names = sorted(n for n, e in reasons.items() if e["class"] == klass)
        if not names:
            continue
        if lines:
            lines.append("")
        lines += [f"**{title}**", "", "| Reason | What it means | Who acts |", "|---|---|---|"]
        for n in names:
            e = reasons[n]
            summary = e["summary"].replace("|", "\\|")
            lines.append(f"| `{n}` | {summary} | {ACTORS.get(e['actor'], e['actor'])} |")
    return "\n".join(lines)


def main():
    check = "--check" in sys.argv[1:]
    if not GUIDE.exists():
        if check:
            return
        sys.exit(f"{GUIDE.relative_to(ROOT)} does not exist")
    text = GUIDE.read_text(encoding="utf-8")
    if BEGIN not in text or END not in text:
        sys.exit(f"{GUIDE.relative_to(ROOT)}: markers {BEGIN} / {END} missing")
    head, rest = text.split(BEGIN, 1)
    _, tail = rest.split(END, 1)
    new = f"{head}{BEGIN}\n{table()}\n{END}{tail}"
    if check:
        if new != text:
            print(f"{GUIDE.relative_to(ROOT)}: reasons table is not current; run "
                  f"scripts/gen-reason-table.py")
            sys.exit(1)
        return
    GUIDE.write_text(new, encoding="utf-8")
    print(f"wrote the reasons table of {GUIDE.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
