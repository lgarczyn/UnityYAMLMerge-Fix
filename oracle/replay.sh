#!/bin/sh
# Replay real both-sides-changed merge triples from Trailblazers history
# through a candidate driver and check every output semantically with the
# reference verifier. Hightower only.
#
# usage: oracle/replay.sh <driver-cmd...>
# driver contract: BASE REMOTE LOCAL OUTPUT, git merge-driver order.
# example: oracle/replay.sh ./target/release/uymerge
# example: oracle/replay.sh python3 unityyamlmerge_fix.py
#
# Byte equality with the reference driver is NOT expected once the
# candidate replaces the native tool: outcomes may legitimately improve.
# The gate is: every rc 0 output passes validate_merge against its
# inputs, and no triple regresses from verified-clean to conflict without
# a genuine both-changed reason. Review the summary by hand.
set -eu
ROOT=$(cd "$(dirname "$0")/.." && pwd)
python3 "$ROOT/oracle/replay.py" "$@"
