#!/usr/bin/env bash
# Typecheck every UI workspace and compare the errors with ui/typecheck-baseline.txt.
#
#   typecheck-baseline.sh --write   record the current errors as the baseline
#   typecheck-baseline.sh --check   fail when an error appears that the baseline does not list
#   typecheck-baseline.sh           same as --check
#
# Errors are normalised to "<package>:<file>: <TScode>: <first message line>" without
# line/column numbers, so moving code does not count as a new error. The comparison
# counts duplicates: a second copy of a known error in the same file is a new error.
set -o pipefail

mode="${1:---check}"
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ui="$(cd "$here/.." && pwd)"
baseline="$ui/typecheck-baseline.txt"
tmpdir="$(mktemp -d)"
trap 'rm -rf "$tmpdir"' EXIT

for pkgdir in "$ui"/packages/*/; do
  pkg="$(basename "$pkgdir")"
  [ -f "$pkgdir/tsconfig.json" ] || continue
  (cd "$pkgdir" && "$ui/node_modules/.bin/tsc" --noEmit -p tsconfig.json) > "$tmpdir/$pkg.raw" 2>&1
  python3 -I "$here/typecheck-normalise.py" "$pkg" < "$tmpdir/$pkg.raw" >> "$tmpdir/current.txt"
done
sort "$tmpdir/current.txt" > "$tmpdir/current.sorted"

case "$mode" in
  --write)
    cp "$tmpdir/current.sorted" "$baseline"
    echo "baseline written: $(wc -l < "$baseline") errors -> $baseline"
    ;;
  --check)
    if [ ! -f "$baseline" ]; then echo "no baseline at $baseline (run --write first)" >&2; exit 2; fi
    python3 -I "$here/typecheck-normalise.py" --diff "$baseline" "$tmpdir/current.sorted"
    ;;
  *)
    echo "usage: $0 [--write|--check]" >&2; exit 2 ;;
esac
