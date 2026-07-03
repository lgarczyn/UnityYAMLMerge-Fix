#!/usr/bin/env python3
"""Generate golden files for tests/fixtures from the Python reference.

For every tests/fixtures/inputs/<name> this writes
tests/fixtures/golden/<name>.rewrap and <name>.unwrap. Goldens are
committed; regenerate only when the reference itself changes, and explain
the change in the same commit. Never hand-edit a golden.
"""
import importlib.util
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("uymf", ROOT / "unityyamlmerge_fix.py")
uymf = importlib.util.module_from_spec(spec)
spec.loader.exec_module(uymf)

inputs = ROOT / "tests" / "fixtures" / "inputs"
golden = ROOT / "tests" / "fixtures" / "golden"
golden.mkdir(parents=True, exist_ok=True)
for p in sorted(inputs.iterdir()):
    text = uymf._read(str(p))
    uymf._write(str(golden / (p.name + ".rewrap")), uymf.reserialize(text))
    uymf._write(str(golden / (p.name + ".unwrap")),
                uymf.reserialize(text, uymf.INF, uymf.INF, fix_empty=False))
    print("golden:", p.name)
