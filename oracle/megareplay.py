#!/usr/bin/env python3
"""Whole-history replay: every both-changed smart-merge triple ever merged
on origin/main, run through uymerge, with three independent checks per
triple: the Python reference verifier on every clean output, a side-swap
symmetry pass, and the production stopgap driver for flip-impact data.
Writes progress to /tmp/megareplay.log. Hightower only, long-running."""
import importlib.util
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
REPO = os.environ.get("CORPUS_REPO", os.path.expanduser("~/Projects/Trailblazers-4"))
UYMERGE = sys.argv[1] if len(sys.argv) > 1 else str(ROOT / "target/release/uymerge")
STOPGAP = ["python3", str(ROOT / "unityyamlmerge_fix.py")]

spec = importlib.util.spec_from_file_location("uymf", ROOT / "unityyamlmerge_fix.py")
uymf = importlib.util.module_from_spec(spec)
spec.loader.exec_module(uymf)

EXT = {".meta", ".unity", ".asset", ".prefab", ".mat", ".anim",
       ".controller", ".playable", ".mask", ".spriteatlas"}
LOG = open("/tmp/megareplay.log", "w", buffering=1)


def log(msg):
    LOG.write(msg + "\n")


def git(*a):
    return subprocess.run(["git", "-C", REPO] + list(a),
                          capture_output=True, text=True).stdout


def blob(rev, f, dest):
    r = subprocess.run(["git", "-C", REPO, "show", "%s:%s" % (rev, f)],
                       capture_output=True)
    if r.returncode:
        return False
    open(dest, "wb").write(r.stdout)
    return True


def drive(cmd, base, remote, local, out):
    shutil.copyfile(local, out)
    r = subprocess.run(cmd + [base, remote, local, out], capture_output=True,
                       text=True, env=dict(os.environ))
    return r.returncode


def unwrapped(path):
    return uymf.reserialize(uymf._read(path), uymf.INF, uymf.INF,
                            fix_empty=False)


triples = []
for line in git("log", "origin/main", "--merges", "--format=%H %P").splitlines():
    parts = line.split()
    if len(parts) != 3:
        continue
    m, p1, p2 = parts
    base = git("merge-base", p1, p2).strip()
    if not base:
        continue
    f1 = {f for f in git("diff", "--name-only", base, p1).splitlines()
          if os.path.splitext(f)[1] in EXT}
    f2 = {f for f in git("diff", "--name-only", base, p2).splitlines()
          if os.path.splitext(f)[1] in EXT}
    for f in sorted(f1 & f2):
        triples.append((m, base, p2, p1, f))
log("triples collected: %d" % len(triples))

stats = {"clean": 0, "conflict": 0, "invalid": 0, "skip": 0,
         "swap_mismatch": 0, "stop_clean_uy_clean": 0,
         "stop_clean_uy_conflict": 0, "stop_conflict_uy_clean": 0,
         "stop_conflict_uy_conflict": 0}
work = tempfile.mkdtemp(prefix="mega_")
for n, (m, base, remote, local, f) in enumerate(triples):
    if n % 50 == 0:
        log("progress %d/%d %s" % (n, len(triples), stats))
    b = os.path.join(work, "b")
    r = os.path.join(work, "r")
    l = os.path.join(work, "l")
    o = os.path.join(work, "o")
    if not (blob(base, f, b) and blob(remote, f, r) and blob(local, f, l)):
        stats["skip"] += 1
        continue
    uy_rc = drive([UYMERGE], b, r, l, o)
    if uy_rc == 0:
        try:
            viols = uymf.validate_merge(unwrapped(b), unwrapped(l),
                                        unwrapped(r), unwrapped(o))
        except Exception as e:
            viols = ["verifier error %r" % e]
        if viols:
            stats["invalid"] += 1
            log("INVALID %s %s :: %s" % (m[:8], f, "; ".join(viols[:3])))
        else:
            stats["clean"] += 1
    else:
        stats["conflict"] += 1
    # side-swap symmetry: conflict state must not depend on side order
    o2 = os.path.join(work, "o2")
    uy_rc_sw = drive([UYMERGE], b, l, r, o2)
    if (uy_rc == 0) != (uy_rc_sw == 0):
        stats["swap_mismatch"] += 1
        log("SWAP-MISMATCH %s %s rc=%d swapped=%d" % (m[:8], f, uy_rc, uy_rc_sw))
    elif uy_rc_sw == 0:
        try:
            viols = uymf.validate_merge(unwrapped(b), unwrapped(r),
                                        unwrapped(l), unwrapped(o2))
        except Exception as e:
            viols = ["verifier error %r" % e]
        if viols:
            stats["invalid"] += 1
            log("INVALID-SWAPPED %s %s :: %s" % (m[:8], f, "; ".join(viols[:3])))
    # flip impact: the production stopgap on the same triple
    o3 = os.path.join(work, "o3")
    stop_rc = drive(STOPGAP, b, r, l, o3)
    key = "stop_%s_uy_%s" % ("clean" if stop_rc == 0 else "conflict",
                             "clean" if uy_rc == 0 else "conflict")
    stats[key] += 1
    if stop_rc == 0 and uy_rc != 0:
        log("FRICTION %s %s" % (m[:8], f))
shutil.rmtree(work, ignore_errors=True)
log("FINAL %s" % stats)
print("megareplay done:", stats)
