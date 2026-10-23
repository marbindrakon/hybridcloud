"""Print docs/content/terms.yaml as JSON (for ui/scripts/tests/vocabulary.test.mjs).

  python3 -I ui/scripts/terms-json.py > terms.json
"""
import json
import pathlib
import sys

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]

if __name__ == "__main__":
    json.dump(yaml.safe_load((ROOT / "docs/content/terms.yaml").read_text(encoding="utf-8")), sys.stdout)
