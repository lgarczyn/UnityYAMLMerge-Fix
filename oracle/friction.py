#!/usr/bin/env python3
"""Classify megareplay FRICTION lines: triples where the production stopgap
exits clean but uymerge conflicts. For each, validate the stopgap's own
output with the reference verifier. A stopgap result that fails validation
is a caught corruption, not friction; only verified-clean stopgap merges
that uymerge refuses count as real flip friction.

usage: friction.py <megareplay-log>
"""
import importlib.util
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
REPO = os.environ.get("CORPUS_REPO", os.path.expanduser("~/Projects/Trailblazers-4"))
STOPGAP = ["python3", str(ROOT / "unityyamlmerge_fix.py")]

spec = importlib.util.spec_from_file_location("uymf", ROOT / "unityyamlmerge_fix.py")
uymf = importlib.util.module_from_spec(spec)
spec.loader.exec_module(uymf)


def git(*a):
    return subprocess.run(["git", "-C", REPO] + list(a),
                          capture_output=True, text=True).stdout


def unwrapped(path):
    return uymf.reserialize(uymf._read(path), uymf.INF, uymf.INF,
                            fix_empty=False)


cases = []
for line in open(sys.argv[1]):
    m = re.match(r"FRICTION (\S+) (.+)", line.strip())
    if m:
        cases.append((m.group(1), m.group(2)))
print("friction cases to classify:", len(cases))

work = tempfile.mkdtemp(prefix="fric_")
true_friction = caught = broken = 0
by_file = {}
for short, f in cases:
    mh = git("log", "--format=%H", "--max-count=1", short).strip() or short
    ps = git("rev-list", "--parents", "-n1", mh).split()
    if len(ps) < 3:
        continue
    base = git("merge-base", ps[1], ps[2]).strip()
    ok = True
    paths = []
    for name, rev in (("base", base), ("remote", ps[2]), ("local", ps[1])):
        r = subprocess.run(["git", "-C", REPO, "show", "%s:%s" % (rev, f)],
                           capture_output=True)
        if r.returncode:
            ok = False
            break
        p = os.path.join(work, name)
        open(p, "wb").write(r.stdout)
        paths.append(p)
    if not ok:
        continue
    out = os.path.join(work, "out")
    shutil.copyfile(paths[2], out)
    r = subprocess.run(STOPGAP + paths + [out], capture_output=True, text=True)
    if r.returncode != 0:
        broken += 1
        continue
    try:
        viols = uymf.validate_merge(unwrapped(paths[0]), unwrapped(paths[2]),
                                    unwrapped(paths[1]), unwrapped(out))
    except Exception as e:
        viols = ["verifier error %r" % e]
    key = f.split("/")[-1]
    if viols:
        caught += 1
        by_file.setdefault(key, [0, 0])[0] += 1
    else:
        true_friction += 1
        by_file.setdefault(key, [0, 0])[1] += 1
shutil.rmtree(work, ignore_errors=True)

print("\nstopgap clean but INVALID, a caught corruption: %d" % caught)
print("stopgap clean and valid, true flip friction:     %d" % true_friction)
print("not reproducible as stopgap-clean:               %d" % broken)
print("\nper file (caught / friction):")
for k in sorted(by_file, key=lambda x: -sum(by_file[x])):
    print("  %-50s %3d / %3d" % (k[:50], by_file[k][0], by_file[k][1]))
