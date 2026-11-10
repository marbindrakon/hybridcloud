# -*- coding: utf-8 -*-
"""What FRR's running configuration on a border gateway node still lacks of the desired file, read only.

The difference is the post-reload verification's (shc-frr-check, installed on the node by the fabric and gateway
jobs next to shc-fabric-apply): frr-reload.py --test against the file, without the lines that never match by design
and without the realization's nodes.frrLoadCheck.ignore patterns. Lines that check already recorded in its note as
left by the last start or reload (FRR does not take them) are not pending either: a job that reloaded for them would
loop. Nothing is written: neither the note nor the record of what FRR loaded.

As a script (the jobs, on the node: `python3 - --note FILE [--pathspace NAME] [--ignore REGEX]... FILE` with this file
on stdin): one pending line per line, exit 0; nothing printed when nothing is pending or it cannot be told (the
check is not installed on the node, or frr-reload.py --test could not run).
As a function (the fabric health reader runs this file's source): frr_pending_difference(run, pathspace, desired,
note=..., ignore=...) -> [lines] or None (cannot be told).
"""
import argparse
import importlib.machinery
import importlib.util
import os
import sys

CHECKER = "/usr/local/sbin/shc-frr-check"
TIMEOUT = 20


def load_checker(path=CHECKER):
    if not os.path.exists(path):
        return None
    loader = importlib.machinery.SourceFileLoader("shc_frr_check", path)
    spec = importlib.util.spec_from_loader("shc_frr_check", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def frr_pending_difference(run, pathspace, desired, note=None, ignore=(), checker=None, timeout=TIMEOUT):
    """run is unused (the check runs frr-reload.py itself, bounded by `timeout`); kept for the reader's call."""
    try:
        c = checker if checker is not None else load_checker()
    except Exception:  # noqa: BLE001 - an unreadable check tells nothing
        return None
    if c is None:
        return None
    c.TIMEOUT = timeout
    text, err = c.run_test(desired, pathspace, c.RELOAD)
    if err:
        return None
    try:
        with open(desired) as f:
            conf_text = f.read()
    except OSError:
        conf_text = ""
    left = c.left_over(text, list(ignore or ()), conf_text)
    known = c.read_note(note) if note else set()
    return [l for l in left if l not in known]


def main(argv=None):
    ap = argparse.ArgumentParser(description="what FRR's running configuration still lacks of the file")
    ap.add_argument("conf")
    ap.add_argument("--pathspace", default="")
    ap.add_argument("--note", default="")
    ap.add_argument("--ignore", action="append", default=[])
    a = ap.parse_args(argv)
    for line in frr_pending_difference(None, a.pathspace, a.conf, a.note, a.ignore) or []:
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
