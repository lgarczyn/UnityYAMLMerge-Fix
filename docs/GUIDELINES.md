# Programming guidelines

These are binding for every packet. PRs that violate them get reworked.

## Rust

- Pinned stable toolchain via rust-toolchain.toml. No nightly features.
- `#![forbid(unsafe_code)]` at crate root. This is non-negotiable.
- Zero external dependencies in the binary. Std only. Dev-dependencies
  are allowed for tests (proptest) but nothing ships in the release
  binary. If a packet seems to need a crate, stop and flag it in the PR
  instead of adding it.
- No panics reachable from input data: no `unwrap`/`expect`/indexing on
  anything derived from file content. Return `Result` or use checked
  access. `unwrap` is acceptable only on invariants created in the same
  function, with a comment saying why it holds.
- Clippy clean with `-D warnings`. rustfmt with repo defaults. `make
  check` runs both plus tests; it must pass before every commit.
- Follow the reference implementation's structure and names, snake_case.
  Same function boundaries as the Python file so differential debugging
  stays function-by-function. Cleverness that diverges from the
  reference is a bug factory; port first, refactor never (until the
  oracle suite is green and only then with zero behavior change).
- Bug-compatibility is a feature. Where SPEC marks reference behavior as
  bug-compatible, reproduce it and cover it with a test.

## Comments and commit messages

Five rules, applied to every comment and every commit message line:
1. Concise; cut filler.
2. Present only when it clarifies; never restate the code.
3. Lines at most 80 characters.
4. Plain structure: no nested clauses, no parentheses, no em dashes.
5. ASCII only.
Commit subjects: imperative, max 72 chars, prefixed with the packet id,
like `P3: reserialize dispatch loop with byte parity`.

## Testing

- TDD against fixtures: write the failing test from tests/fixtures and
  the golden files first, then implement.
- Every SPEC property listed as "must be property tests" has a proptest.
- A bug found anywhere gets a minimal committed regression test before
  the fix, in the same commit.
- Do not weaken, skip, or delete an existing test to make a packet pass.
  If a test looks wrong, flag it in the PR with your reasoning.
- Golden fixtures are generated only by oracle/gen_goldens.py from the
  Python reference. Never hand-edit a golden.

## Workflow for a lone agent

1. Read docs/PLAN.md, docs/SPEC.md, this file, and CLAUDE.md.
2. Pick the first unchecked packet whose needs are all merged. One
   packet per session. Do not touch other packets' scope.
3. Branch `p<N>-<slug>` from origin/main.
4. Implement with tests. Run `make check`. For P3 and P10 also run the
   oracle scripts (hightower only) and paste their summary output into
   the PR description.
5. Tick the packet checkbox in docs/PLAN.md in the same PR.
6. Push and print the PR link. Never merge your own PR. Never commit
   directly to main.

## Hard rules inherited from the incident history

- A merge tool must never exit 0 with unverified output.
- A failure path must never leave OUTPUT looking like a clean file.
- Silent side-picks on both-changed values are data loss, treat as
  conflicts.
- Byte fidelity beats prettiness: output bytes match the editor, even
  where the editor's format is odd (trailing spaces, fold quirks).
