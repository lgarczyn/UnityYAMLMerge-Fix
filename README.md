# UnityYAMLMerge-Fix

> **Roadmap:** this wrapper is the production stopgap. Its replacement,
> `uymerge` — a dependency-free Rust binary that merges Unity YAML
> structurally instead of wrapping the native tool — is under construction
> in this repo. See `docs/PLAN.md` (work packets), `docs/SPEC.md`
> (behavior), `docs/GUIDELINES.md` (rules). Agents: read `CLAUDE.md` first.

A git merge driver that makes Unity's Smart Merge produce output **identical to what the Unity
editor writes**. The stock `UnityYAMLMerge` merges scenes, prefabs and assets well, but its output
diverges from the editor, so merged files churn and can silently lose data. This fixes that:

- significant whitespace dropped before a fold (silent data loss)
- scalars wrapped at the wrong width
- CRLF flattened to LF
- empty flow values rewritten as `''`, flow mappings re-wrapped, bare-sequence scalars not folded

Two equivalent single-file implementations, byte-verified against each other:

- `unityyamlmerge_fix.py` — the reference. Pure Python, standard library only.
- `UnityYamlMergeFix.cs` / `uymf.exe` — a C# port compiled to one platform-neutral IL exe that
  runs on the Mono runtime bundled with every Unity editor, for machines without Python (a
  missing `python3` on Windows makes git silently keep "ours" with no conflict markers, which
  is how string tables lose merged work).

`yamlmerge-driver.sh` chains them: the editor's bundled mono + `uymf.exe`, else a system
`mono` (for CI runners using a standalone UnityYAMLMerge), else `python3` if the Python file
is deployed next to the script, else a plain native merge — the driver itself never fails
from a missing runtime. Deployments only need the script and `uymf.exe`; the Python file is
optional at runtime and kept here as the reference implementation.

## How it works

It runs the native `UnityYAMLMerge`, but unwraps the inputs first so the standard-YAML parser can't
strip fold-trailing whitespace, then re-wraps the result exactly as the editor would and restores
the original line endings. If anything fails it falls back to a plain native merge, so it can never
break a merge. Validated to match the live editor's serialization across a 7,400-file project and a
12k-file cross-repo corpus.

## Requirements

- Python 3
- A Unity install. The script auto-detects `~/Unity/Hub/Editor/*/Editor/Data/Tools/UnityYAMLMerge`;
  otherwise set `UNITY_YAML_MERGE` to its full path.

## Install

1. Put `unityyamlmerge_fix.py` somewhere, e.g. `~/bin/`.

2. Register it as a git merge driver (add `--global` to apply to all repos):

   ```
   git config merge.unityyaml.name   "Unity YAML editor-faithful merge"
   git config merge.unityyaml.driver "python3 /path/to/unityyamlmerge_fix.py %O %B %A %A"
   ```

3. Route Unity files to it with `.gitattributes` (commit this in your repo):

   ```
   *.unity   merge=unityyaml
   *.prefab  merge=unityyaml
   *.asset   merge=unityyaml
   ```

That is all. Merges of scenes, prefabs and assets now come out editor-clean.

## Structural verification (no silent loss)

The native tool is line-based: dense edits near long multi-line scalars can drop, duplicate, or
misalign records and still exit 0 (observed in real history: dropped `m_Id` entries, duplicated
entries, duplicated SerializeReference records with divergent content). Every merge result is
therefore verified against true 3-way semantics before the driver reports success:

- string-table entries by `m_Id`: text and metadata rid set each follow ours/theirs/base rules,
  including "both sides changed differently must conflict, never silently pick one"
- SerializeReference records by `rid`: payload plus the `m_SharedEntries` id list (merged as a
  set, honoring both sides' additions and removals) -- this is where smart-string flags live
- whole YAML documents by `&anchor` (prefabs, scenes)
- no dangling rid references, no new duplicate ids; byte-identical duplicate records (a known
  native churn) are repaired by dedup, inherited input corruption is tolerated but never grown

On any violation the driver retries with a plain text merge and accepts it only if it verifies;
otherwise it fails with conflict markers (or a whole-file ours/theirs conflict), because git
keeps the driver's output on failure and a clean-looking lossy file gets committed as-is. A zero
exit is a verified merge.

Verification requires decodable UTF-8 inputs; content inside non-table documents that both sides
edited is merged by the native tool and checked at record granularity, not field-by-field.

## Build and tests

```
MB=~/Unity/Hub/Editor/<version>/Editor/Data/MonoBleedingEdge
"$MB/bin/mono" "$MB/lib/mono/4.5/csc.exe" -nologo -optimize+ -out:uymf.exe UnityYamlMergeFix.cs
python3 -m pytest tests/
```

The suite covers the dropped-entry guard, malformed-table rejection, the driver fallback chain,
codec invariants on the editor's serialization quirks, and Python/C# parity (parity tests skip
when no Unity mono is available).

## Notes

- `UNITY_YAML_MERGE=/path/to/UnityYAMLMerge` overrides auto-detection (e.g. a non-default install).
- The format constants (plain width 79, quoted width 80, inline flow mappings) match the editor's
  default. They are at the top of the file if a future Unity version ever changes them.
- Manual use: `python3 unityyamlmerge_fix.py BASE REMOTE LOCAL OUTPUT` (exit 0 = clean, non-zero =
  conflicts, handled like any merge driver).
