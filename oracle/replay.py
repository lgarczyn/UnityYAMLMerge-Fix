#!/usr/bin/env python3
"""Backend for replay.sh: extract both-sides-changed smart-merge triples
from recent Trailblazers history, run the candidate driver on each, and
verify every clean output with the reference validate_merge."""
import importlib.util
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
REPO = os.environ.get("CORPUS_REPO", os.path.expanduser("~/Projects/Trailblazers-4"))
SINCE = os.environ.get("REPLAY_SINCE", "2026-03-01")
DRIVER = sys.argv[1:]
assert DRIVER, "usage: replay.py <driver-cmd...>"

spec = importlib.util.spec_from_file_location("uymf", ROOT / "unityyamlmerge_fix.py")
uymf = importlib.util.module_from_spec(spec)
spec.loader.exec_module(uymf)

EXT = {".meta", ".unity", ".asset", ".prefab", ".mat", ".anim",
       ".controller", ".playable", ".mask", ".spriteatlas"}


def git(*a):
    return subprocess.run(["git", "-C", REPO] + list(a),
                          capture_output=True, text=True).stdout


def files_changed(a, b):
    return {f for f in git("diff", "--name-only", a, b).splitlines()
            if os.path.splitext(f)[1] in EXT}


triples = []
for line in git("log", "origin/main", "--merges", "--format=%H %P",
                "--since=" + SINCE).splitlines():
    parts = line.split()
    if len(parts) != 3:
        continue
    m, p1, p2 = parts
    base = git("merge-base", p1, p2).strip()
    if not base:
        continue
    for f in sorted(files_changed(base, p1) & files_changed(base, p2)):
        triples.append((base, p2, p1, f))
        if len(triples) >= 120:
            break
    if len(triples) >= 120:
        break

work = tempfile.mkdtemp(prefix="replay_")
clean = conflict = invalid = skipped = 0
for n, (base, remote, local, f) in enumerate(triples):
    paths = []
    ok = True
    for name, rev in (("base", base), ("remote", remote), ("local", local)):
        r = subprocess.run(["git", "-C", REPO, "show", "%s:%s" % (rev, f)],
                           capture_output=True)
        if r.returncode:
            ok = False
            break
        p = os.path.join(work, "%d.%s" % (n, name))
        open(p, "wb").write(r.stdout)
        paths.append(p)
    if not ok:
        skipped += 1
        continue
    out = os.path.join(work, "%d.out" % n)
    shutil.copyfile(paths[2], out)
    r = subprocess.run(DRIVER + paths + [out], capture_output=True, text=True)
    if r.returncode != 0:
        conflict += 1
        print("conflict rc=%d  %s  %s" % (r.returncode, base[:10], f))
        continue
    try:
        viols = uymf.validate_merge(
            *(uymf.reserialize(uymf._read(p), uymf.INF, uymf.INF, fix_empty=False)
              for p in (paths[0], paths[2], paths[1], out)))
    except Exception as e:
        viols = ["verifier error: %r" % e]
    if viols:
        invalid += 1
        print("INVALID  %s  %s" % (base[:10], f))
        for v in viols[:4]:
            print("   - " + v)
    else:
        clean += 1
shutil.rmtree(work, ignore_errors=True)
print("\nreplay: %d triples, %d verified clean, %d conflicts, %d INVALID, %d skipped"
      % (len(triples), clean, conflict, invalid, skipped))
sys.exit(1 if invalid else 0)
