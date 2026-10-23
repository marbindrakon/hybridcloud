#!/usr/bin/env python3
"""Fill the common descriptions that are missing in the CRD sources, then regenerate crds.yaml.

Same as `python3 -I scripts/crd-descriptions.py fill`; kept under this name because the
integration steps call it. Idempotent: a second run changes nothing.
"""
import subprocess
import sys
from pathlib import Path

tool = Path(__file__).resolve().parent / "crd-descriptions.py"
sys.exit(subprocess.run([sys.executable, "-I", str(tool), "fill", *sys.argv[1:]]).returncode)
