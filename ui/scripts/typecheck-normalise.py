"""Normalise tsc output and compare it with a baseline (used by typecheck-baseline.sh).

  typecheck-normalise.py <package> < tsc-output      print normalised error lines
  typecheck-normalise.py --diff <baseline> <current> exit 1 when current has errors the baseline lacks
"""
import collections
import re
import sys

ERR = re.compile(r"^(?P<file>[^\s(][^(]*)\((?P<line>\d+),(?P<col>\d+)\): error (?P<code>TS\d+): (?P<msg>.*)$")


def normalise(pkg, stream):
    for raw in stream:
        m = ERR.match(raw.rstrip("\n"))
        if not m:
            m2 = re.match(r"^error (TS\d+): (.*)$", raw.strip())
            if m2:
                print(f"{pkg}:-: {m2.group(1)}: {m2.group(2).strip()}")
            continue
        msg = re.sub(r"/[^\s'\"]*/node_modules/", "node_modules/", m.group("msg").strip())
        print(f"{pkg}:{m.group('file')}: {m.group('code')}: {msg}")


def diff(baseline_path, current_path):
    with open(baseline_path) as f:
        base = collections.Counter(l.rstrip("\n") for l in f if l.strip())
    with open(current_path) as f:
        cur = collections.Counter(l.rstrip("\n") for l in f if l.strip())
    new = cur - base
    gone = base - cur
    print(f"baseline {sum(base.values())} errors, current {sum(cur.values())} errors, "
          f"new {sum(new.values())}, fixed {sum(gone.values())}")
    for line, n in sorted(gone.items()):
        print(f"  fixed ({n}): {line}")
    for line, n in sorted(new.items()):
        print(f"  NEW   ({n}): {line}")
    if new:
        print("typecheck: new errors against the baseline", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    if sys.argv[1] == "--diff":
        sys.exit(diff(sys.argv[2], sys.argv[3]))
    normalise(sys.argv[1], sys.stdin)
