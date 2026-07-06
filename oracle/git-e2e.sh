#!/bin/sh
# End-to-end through real git: register uymerge as the merge driver in a
# scratch repo and drive actual git merges, the way designer machines will
# run it. Verifies the %O %B %A %A contract, in-place output, exit-code
# handling, conflict surfacing and CRLF fidelity under git itself.
#
# usage: oracle/git-e2e.sh <path-to-uymerge>
set -eu
UYMERGE=$(cd "$(dirname "$1")" && pwd)/$(basename "$1")
[ -x "$UYMERGE" ] || { echo "no binary at $UYMERGE"; exit 1; }
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
cd "$WORK"

git init -q repo && cd repo
git config user.name t && git config user.email t@t
git config merge.uymerge.name "uymerge structural merge"
git config merge.uymerge.driver "'$UYMERGE' %O %B %A %A"
printf '*.asset -text merge=uymerge\n' > .gitattributes

asset() {
  printf '%%YAML 1.1\n%%TAG !u! tag:unity3d.com,2011:\n--- !u!114 &11400000\nMonoBehaviour:\n  m_Name: T\n  m_TableData:\n'
  for e in "$@"; do
    id=${e%%=*}; text=${e#*=}
    printf '  - m_Id: %s\n    m_Localized: %s\n    m_Metadata:\n      m_Items: []\n' "$id" "$text"
  done
  printf '  references:\n    version: 2\n'
}

fail() { echo "FAIL: $1"; exit 1; }

# scenario 1: disjoint entry edits merge clean through git
asset 100=alpha 200=beta > t.asset
git add -A && git commit -qm base
git checkout -qb side
asset 100=alpha 200=beta-side > t.asset
git commit -qam side
git checkout -q master 2>/dev/null || git checkout -q main
asset 100=alpha-main 200=beta > t.asset
git commit -qam main-edit
git merge -q --no-edit side >/dev/null 2>&1 || fail "clean merge exited non-zero"
grep -q 'alpha-main' t.asset || fail "ours edit lost"
grep -q 'beta-side' t.asset || fail "theirs edit lost"
git diff --quiet || fail "worktree dirty after clean merge"
echo "PASS git clean merge, both edits present"

# scenario 2: same-entry conflict surfaces as a git conflict with markers
git checkout -qb side2
asset 100=alpha-main 200=beta-two > t.asset
git commit -qam side2
git checkout -q - >/dev/null 2>&1
asset 100=alpha-main 200=beta-one > t.asset
git commit -qam main-two
if git merge -q --no-edit side2 >/dev/null 2>&1; then fail "conflict merged clean"; fi
git status --porcelain | grep -q '^UU t.asset' || fail "file not marked conflicted"
grep -q '^<<<<<<< ours' t.asset || fail "no markers in conflicted file"
grep -q 'beta-one' t.asset && grep -q 'beta-two' t.asset || fail "conflict sides missing"
git merge --abort
echo "PASS git conflict has markers and UU status"

# scenario 3: CRLF asset stays CRLF through a git merge
asset 300=word | awk '{printf "%s\r\n", $0}' > c.asset
git add c.asset && git commit -qm crlf-base
git checkout -qb side3
sed -i.bak 's/word/word-side/' c.asset && rm -f c.asset.bak
git commit -qam side3
git checkout -q -
git merge -q --no-edit side3 >/dev/null 2>&1 || fail "crlf merge failed"
cr=$(tr -cd '\r' < c.asset | wc -c)
nl=$(tr -cd '\n' < c.asset | wc -c)
grep -q 'word-side' c.asset || fail "crlf edit lost"
[ "$cr" -eq "$nl" ] || fail "crlf fidelity lost through git"
echo "PASS git crlf merge byte-faithful"

echo "git-e2e: all scenarios pass"
