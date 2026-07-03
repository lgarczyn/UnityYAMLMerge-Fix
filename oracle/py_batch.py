#!/usr/bin/env python3
"""Reference-side batch harness for differential.sh: run the Python
reference's reserialize over a file list, one output per input, named by
list index, .error suffix on decode failure. Mirrors the --batch modes."""
import importlib.util
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("uymf", ROOT / "unityyamlmerge_fix.py")
uymf = importlib.util.module_from_spec(spec)
spec.loader.exec_module(uymf)

mode, listfile, outdir = sys.argv[1], sys.argv[2], sys.argv[3]
os.makedirs(outdir, exist_ok=True)
for i, p in enumerate(open(listfile).read().split("\n")):
    if not p:
        continue
    try:
        text = uymf._read(p)
        if mode == "--batch-reserialize":
            r = uymf.reserialize(text)
        else:
            r = uymf.reserialize(text, uymf.INF, uymf.INF, fix_empty=False)
        uymf._write(os.path.join(outdir, str(i)), r)
    except Exception:
        uymf._write(os.path.join(outdir, "%d.error" % i), "")
