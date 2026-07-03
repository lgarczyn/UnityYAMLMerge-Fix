#!/bin/sh
# Private-corpus differential: byte-compare a candidate implementation
# against the Python reference over every Unity YAML file in a local
# Trailblazers checkout, in both batch modes. Hightower only.
#
# usage: oracle/differential.sh <candidate-cmd...>
# candidate must support --batch-reserialize LIST OUTDIR and
# --batch-unwrap LIST OUTDIR, like the reference and uymerge do.
# example: oracle/differential.sh ./target/release/uymerge
set -eu
ROOT=$(cd "$(dirname "$0")/.." && pwd)
CORPUS=${CORPUS_REPO:-$HOME/Projects/Trailblazers-4}
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

find "$CORPUS/Assets" -type f \( -name '*.asset' -o -name '*.unity' \
  -o -name '*.prefab' -o -name '*.mat' -o -name '*.anim' \
  -o -name '*.controller' -o -name '*.meta' -o -name '*.spriteatlas' \
  -o -name '*.playable' -o -name '*.mask' \) | sort > "$WORK/list.txt"
echo "corpus: $(wc -l < "$WORK/list.txt") files"

for mode in --batch-reserialize --batch-unwrap; do
  python3 "$ROOT/oracle/py_batch.py" "$mode" "$WORK/list.txt" "$WORK/ref$mode"
  "$@" "$mode" "$WORK/list.txt" "$WORK/got$mode"
  if diff -rq "$WORK/ref$mode" "$WORK/got$mode" > "$WORK/diff$mode.txt"; then
    echo "$mode: identical"
  else
    echo "$mode: DIFFERS ($(wc -l < "$WORK/diff$mode.txt") files)"
    head -5 "$WORK/diff$mode.txt"
    exit 1
  fi
done
