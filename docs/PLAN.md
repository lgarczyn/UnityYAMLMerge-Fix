# uymerge: project plan

The goal: replace UnityYAMLMerge with `uymerge`, a structural 3-way merge tool
for Unity YAML, written in Rust. One static binary per platform. No runtime
dependencies, no wrapper script, no native tool. Registered directly:

```
git config merge.unityyamlmerge.driver "'<path>/uymerge' %O %B %A %A"
```

Read docs/SPEC.md before any packet. Read docs/GUIDELINES.md before writing
code. The Python file `unityyamlmerge_fix.py` is the executable reference for
the codec and the verification semantics; when SPEC prose and reference code
disagree, the reference code wins and the discrepancy must be reported.

## Why this design is already de-risked

- The codec is proven: byte-idempotent over a 22,879-file real corpus, ported
  once already (Python to C#) with zero byte diffs.
- The merge semantics are proven: `validate_merge` in the reference encodes
  them as a checker; this project inverts the same rules into a constructor.
- The oracle suite exists: corpus differential, replayed history merges,
  a red-team battery of fabricated native-tool corruptions.

## Architecture

Single crate `uymerge`, modules with strict one-way dependencies:

```
codec  -> nothing        editor-faithful unwrap/rewrap of raw text
model  -> nothing        parse docs, table entries, RefIds records from text
diff3  -> nothing        plain 3-way line merge, hand-rolled, no crates
merge  -> model, diff3   keyed 3-way constructor + per-doc line merge
verify -> model          self-check of merged output (ported validate_merge)
cli    -> all            driver contract, conflict emission, exit codes
```

The tool works on raw text lines, never on decoded YAML values. Byte
equality with the Unity editor's own serialization is the oracle. This is
NOT a YAML library and must never become one.

## Merge pipeline (what the finished tool does)

1. Read BASE, THEIRS (remote), OURS (local). Record per-file CRLF state.
2. Unwrap all three (codec, width = infinite) so folded scalars align.
3. Split each into documents by `&anchor`. Merge the document SET by
   presence rules. For each document present in the output:
   - If the doc contains `m_TableData` or `references/RefIds`: merge those
     record sets by the keyed rules (SPEC section 4). Non-record lines of
     the doc merge by diff3.
   - Otherwise: diff3 the whole document.
4. Rewrap the result (codec, editor widths). Restore CRLF.
5. Run `verify` on the result against the three inputs. A verification
   failure here is a bug, not a conflict: emit a whole-file conflict and
   exit 1. Exit 0 only on a verified, conflict-free merge.
6. Any conflict: standard `<<<<<<< ours` markers, exit 1.

## Work packets

Each packet is one agent session. Do not start a packet whose "needs" are
not merged. Every packet lands with its tests, green `make check`, and an
updated checkbox here. Packet numbers are stable ids; reference them in
commit messages as `P3:`.

- [x] P1  codec: terminators and plain scalars.
      Implement `split_lines`, `reemit_plain`, `join_plain_value`,
      `gather_continuations` per SPEC 2.1-2.3. Unit tests from fixtures
      plus property tests: rewrap idempotence, unwrap losslessness.
      Needs: nothing.
- [x] P2  codec: quoted scalars and flow cleanup.
      `gather_quoted`, single and double quoted decode/reemit, mixed
      terminator passthrough, `EMPTY_FLOW` strip per SPEC 2.4-2.6.
      Needs: P1.
- [ ] P3  codec: reserialize dispatch loop, byte parity.
      The full `reserialize(text, width, quoted_width, fix_empty)` per
      SPEC 2.7. Acceptance: `oracle/gen_goldens.py` fixtures match byte
      for byte in both modes, and on the hightower box
      `oracle/differential.sh` reports zero diffs over the private corpus.
      Needs: P1, P2.
- [ ] P4  model: parsers.
      Documents by anchor, table entries by m_Id, RefIds records by rid,
      duplicate tracking, spans (line ranges) for reassembly. Mirrors the
      reference parsers exactly (SPEC 3). Needs: P1 only for line utils.
- [ ] P5  diff3: plain 3-way line merge.
      Hand-rolled, std only: LCS-based two-way diff, three-way compose,
      conflict hunks with ours/base/theirs labels. Must match
      `git merge-file` outcomes on the committed diff3 fixture set
      (generate fixtures with git itself via oracle/gen_diff3_cases.sh).
      Needs: nothing.
- [ ] P6  merge: keyed record merge.
      Presence, scalar, and set rules as constructors per SPEC 4. Emits
      per-record conflicts as marker hunks. Table section and RefIds
      section reassembly preserving record order (SPEC 4.5).
      Needs: P4, P5.
- [ ] P7  merge: document-level composition.
      Document set merge, per-document dispatch (keyed sections vs plain
      diff3), whole-file assembly. Needs: P6.
- [ ] P8  verify: port validate_merge.
      Same rules as merge but as a checker over the final output; wired as
      a mandatory post-merge self-check. Needs: P4.
- [ ] P9  cli: driver contract.
      Arg parsing (BASE REMOTE LOCAL OUTPUT), CRLF restore, conflict file
      emission, exit codes per SPEC 5, `--batch-reserialize` and
      `--batch-unwrap` test modes matching the reference harness.
      Needs: P3, P7, P8.
- [ ] P10 oracle green: full differential run.
      On hightower: corpus differential, the 65 replayed history triples
      (rc and verified-content equivalence, not byte equality with the
      native tool), red-team battery 0/9 silent, no-op sweep byte
      identity. Fix regressions. Needs: P9.
- [ ] P11 performance.
      Benchmark vs native UnityYAMLMerge on the largest corpus files.
      Budget: within 2x of native on GraphStrings_en. Optimize only after
      measuring. Needs: P10.
- [ ] P12 fuzzing.
      cargo-fuzz targets: reserialize (idempotence + no panic on arbitrary
      bytes), full merge (no panic, exit contract upheld). Run 1 hour
      locally, fix findings. Needs: P9.
- [ ] P13 release and rollout.
      Tag-triggered release workflow builds linux-musl, windows-msvc,
      mac universal binaries. Update SmartMergeRegistrar.cs and the
      update-from-main workflow in Trailblazers to invoke uymerge
      directly. Runs BEHIND the existing driver first: registrar keeps the
      old chain, CI compares uymerge output on every real merge for a soak
      period, then flips. Needs: P10, P11.

## Testing tiers

1. Unit and property tests: committed, run in CI on every push, on both
   ubuntu and windows runners.
2. Golden fixtures: synthetic inputs in tests/fixtures, goldens generated
   from the Python reference by oracle/gen_goldens.py, committed.
3. Differential oracle: private, local to the hightower box, driven by
   oracle/*.sh against the Trailblazers corpus and the Python reference.
   Cannot run in CI; P3 and P10 acceptance requires running it locally.
4. Red-team battery: oracle/redteam.py, committed, synthetic only.

## Deferred to v2 (decided 2026-07-03, revisit with production evidence)

A tree-and-rules architecture was considered: parse to a Unity-subset tree
with verbatim fallback nodes, merge as rules over nodes, emit through
post-rules. It generalizes merging to every field of every document and
opens per-asset customization, at the cost of a fail-open middle layer and
a materially bigger build. Every failure observed in history lives in
keyed records, which v1 already merges structurally, so v1 ships first.
P1-P5 and P8 plus the oracle are identical in both architectures; the
tree would grow out of the model layer without rewriting the foundation.

Also deferred, as the first customer of that rules layer: rekey-on-insert
collision. When two branches independently add records with colliding
sequential rids, as the Dialogue assets allocate, mint a fresh rid for
one side and rewrite its references instead of conflicting. Opt-in per
asset class only; the same signature can be a cherry-picked record edited
on one side, where rekeying would fork one object into two. The common
benign variant, the same record added identically on both sides, is
already handled by v1 dedup and pinned by red-team scenario S10.

## Out of scope

- Full YAML parsing or emitting from a data model.
- Merging non-Unity YAML.
- Windows-editor integration beyond the registrar line.
- The C# and Python wrappers: frozen as reference and stopgap, no new
  features. Bug fixes found during porting go to the Python reference
  first, then C#, with a failing test in both suites.
