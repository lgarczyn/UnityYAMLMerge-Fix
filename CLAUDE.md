# UnityYAMLMerge-Fix / uymerge

Two things live here:

1. The production stopgap: `unityyamlmerge_fix.py` (reference
   implementation), `UnityYamlMergeFix.cs` + `uymf.exe` (C# port that runs
   on Unity's bundled Mono), `yamlmerge-driver.sh` (fallback chain).
   Frozen except for bug fixes; fixes land in the Python reference first
   with a failing test, then get mirrored to C# and rebuilt.
2. The replacement under construction: `uymerge`, a dependency-free Rust
   binary that merges Unity YAML structurally. All new work happens here.

Start every session by reading docs/PLAN.md (what to build, packet list),
docs/SPEC.md (behavior), docs/GUIDELINES.md (how to build it). Work one
packet per session, on a branch, per the workflow in GUIDELINES.

Commands:
- `make check` — fmt, clippy, full test suite. Must pass before commits.
- `python3 -m pytest tests/` — the stopgap's suite; keep green too.
- `oracle/differential.sh`, `oracle/replay.sh`, `oracle/redteam.py` —
  private-corpus oracle, only on the hightower box; required for P3/P10.

The C# exe rebuild, if you fix the stopgap:
```
MB=~/Unity/Hub/Editor/2022.3.62f2/Editor/Data/MonoBleedingEdge
"$MB/bin/mono" "$MB/lib/mono/4.5/csc.exe" -nologo -optimize+ -out:uymf.exe UnityYamlMergeFix.cs
```

Vendoring: Trailblazers consumes Tools/SmartMerge (script + exe + cs).
After stopgap changes, copy those files there and note it in the PR.
