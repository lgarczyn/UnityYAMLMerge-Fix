# UnityYAMLMerge-Fix

> **This project has been superseded by [uymerge](https://github.com/lgarczyn/uymerge).**
>
> `uymerge` is a dependency-free Rust binary that merges Unity YAML
> **structurally** — no Python, no Mono, no native `UnityYAMLMerge`. For any
> new setup, use it instead. This repository is kept as the frozen reference
> implementation of the original stopgap.

`unityyamlmerge_fix.py` is a git merge driver that makes Unity's bundled
`UnityYAMLMerge` produce output **identical to what the Unity editor writes**.
The stock tool merges scenes, prefabs, and assets well, but its output diverges
from the editor, so merged files churn and can silently lose data. This fixes
that:

- significant whitespace dropped before a fold (silent data loss)
- scalars wrapped at the wrong width
- CRLF flattened to LF
- empty flow values rewritten as `''`, flow mappings re-wrapped, bare-sequence
  scalars not folded

It runs the native `UnityYAMLMerge`, but unwraps the inputs first so the
standard-YAML parser can't strip fold-trailing whitespace, then re-wraps the
result exactly as the editor would and restores the original line endings. If
anything fails it falls back to a plain native merge, so it can never break a
merge. Every result is also verified against true 3-way semantics before
success is reported, so a clean-looking lossy merge is never committed.

## Requirements

- Python 3
- A Unity install. The script auto-detects
  `~/Unity/Hub/Editor/*/Editor/Data/Tools/UnityYAMLMerge`; otherwise set
  `UNITY_YAML_MERGE` to its full path.

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

Merges of scenes, prefabs, and assets now come out editor-clean.

## Manual use

```
python3 unityyamlmerge_fix.py BASE REMOTE LOCAL OUTPUT
```

Exit `0` is a verified, conflict-free merge; a non-zero exit leaves conflict
markers, handled like any merge driver.

## Notes

- `UNITY_YAML_MERGE=/path/to/UnityYAMLMerge` overrides auto-detection.
- The format constants (plain width 79, quoted width 80, inline flow mappings)
  match the editor default and are at the top of the file, should a future
  Unity version ever change them.

## License

MIT — see [LICENSE](LICENSE).
