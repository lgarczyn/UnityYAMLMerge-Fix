#!/bin/sh
# Git merge driver for Unity YAML assets. Runs the C# wrapper uymf.exe on the
# Mono runtime bundled with every Unity editor, so no extra runtime is needed.
# Falls back to the Python reference, then to a plain native UnityYAMLMerge.
#
# A missing runtime must NEVER fail the driver. When a merge driver exits
# non-zero, git leaves the working tree file as OURS with no conflict markers.
# A "resolved" commit then silently reverts the other side's changes. Exactly
# that failure, python3 absent on Windows, stomped main's localization work in
# July 2026. The fallback chain exists so a real merge always runs.
#
# Registered by SmartMergeRegistrar.cs as:
#   sh '<this>' '<UnityYAMLMerge>' %O %B %A %A
set -u
tool=$1; shift
export UNITY_YAML_MERGE="$tool"
dir=$(dirname "$0")
data=$(dirname "$(dirname "$tool")")
for mono in "$data/MonoBleedingEdge/bin/mono" "$data/MonoBleedingEdge/bin/mono.exe"; do
  if [ -f "$dir/uymf.exe" ] && [ -x "$mono" ]; then
    exec "$mono" "$dir/uymf.exe" "$@"
  fi
done
if python3 -c '' 2>/dev/null; then
  exec python3 "$dir/unityyamlmerge_fix.py" "$@"
fi
exec "$tool" merge -h -p --force "$@"
