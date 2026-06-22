# UnityYAMLMerge-Fix

A git merge driver that makes Unity's Smart Merge produce output **identical to what the Unity
editor writes**. The stock `UnityYAMLMerge` merges scenes, prefabs and assets well, but its output
diverges from the editor, so merged files churn and can silently lose data. This fixes that:

- significant whitespace dropped before a fold (silent data loss)
- scalars wrapped at the wrong width
- CRLF flattened to LF
- empty flow values rewritten as `''`, flow mappings re-wrapped, bare-sequence scalars not folded

Pure Python, standard library only, **one file**. Works on every platform and every Unity version
(it drives the `UnityYAMLMerge` that ships with your editor).

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

## Notes

- `UNITY_YAML_MERGE=/path/to/UnityYAMLMerge` overrides auto-detection (e.g. a non-default install).
- The format constants (plain width 79, quoted width 80, inline flow mappings) match the editor's
  default. They are at the top of the file if a future Unity version ever changes them.
- Manual use: `python3 unityyamlmerge_fix.py BASE REMOTE LOCAL OUTPUT` (exit 0 = clean, non-zero =
  conflicts, handled like any merge driver).
