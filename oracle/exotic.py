#!/usr/bin/env python3
"""Exotic-scenario battery: strange but reachable driver situations. Every
scenario must end verified-clean with the exact expected content, or loud
with a non-zero exit; a silent wrong result fails the battery.

usage: exotic.py <driver-cmd...>   default: the release binary
"""
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
DRIVER = sys.argv[1:] or [str(ROOT / "target" / "release" / "uymerge")]

HDR = ("%YAML 1.1\n%TAG !u! tag:unity3d.com,2011:\n--- !u!114 &11400000\n"
       "MonoBehaviour:\n  m_Name: Table_en\n")


def entry(eid, text, rids=()):
    md = ("      m_Items: []" if not rids else
          "      m_Items:\n" + "\n".join("      - rid: %s" % r for r in rids))
    return "  - m_Id: %s\n    m_Localized: %s\n    m_Metadata:\n%s" % (eid, text, md)


def table(*pairs):
    body = "\n".join(entry(i, t) for i, t in pairs)
    return HDR + "  m_TableData:\n" + body + "\n  references:\n    version: 2\n"


def run(base, theirs, ours, binary=False):
    d = tempfile.mkdtemp(prefix="exotic_")
    try:
        paths = []
        for name, data in (("base", base), ("remote", theirs), ("local", ours)):
            p = os.path.join(d, name)
            mode = "wb" if binary else "w"
            with open(p, mode, **({} if binary else {"newline": ""})) as f:
                f.write(data)
            paths.append(p)
        out = os.path.join(d, "out")
        shutil.copyfile(paths[2], out)
        r = subprocess.run(DRIVER + paths + [out], capture_output=True, text=True)
        with open(out, "rb") as f:
            return r.returncode, f.read()
    finally:
        shutil.rmtree(d, ignore_errors=True)


FAILURES = []


def check(name, ok, detail=""):
    print("%-52s %s%s" % (name, "PASS" if ok else "FAIL", "  " + detail if detail and not ok else ""))
    if not ok:
        FAILURES.append(name)


# E1 add/add: empty base, both sides created the file
rc, out = run("", table((100, "ours words")), table((100, "ours words")))
check("E1a add/add identical content is clean", rc == 0 and b"ours words" in out)
rc, out = run("", table((100, "theirs words")), table((200, "ours words")))
check("E1b add/add different entries unions", rc == 0 and b"m_Id: 100" in out and b"m_Id: 200" in out)
rc, out = run("", table((100, "theirs text")), table((100, "ours text")))
check("E1c add/add same id different text conflicts", rc != 0)
rc, out = run("", "", table((100, "a")))
check("E1d theirs empty, ours added: keeps ours", rc == 0 and b"m_Id: 100" in out)
rc, out = run("", "", "")
check("E1e all empty stays empty and clean", rc == 0 and out == b"")

# E2 encodings: UTF-16 input, UTF-8 BOM, raw binary
u16 = table((100, "text")).encode("utf-16")
rc, out = run(table((100, "text")).encode(), u16, table((100, "text")).encode(), binary=True)
check("E2a utf-16 side is loud, never silent", rc != 0)
bom = ("﻿" + table((100, "a")))
rc, out = run(bom, bom, bom)
check("E2b utf-8 bom no-op is byte identical", rc == 0 and out == bom.encode())
blob = bytes(range(256)) * 4
rc, out = run(blob, blob, table((100, "a")).encode(), binary=True)
check("E2c binary garbage is loud", rc != 0)

# E3 shapes: no trailing newline, header only, plain text file
nt = table((100, "a")).rstrip("\n")
rc, out = run(nt, nt, nt)
check("E3a no trailing newline no-op byte identical", rc == 0 and out == nt.encode())
rc, out = run(HDR, HDR, HDR)
check("E3b header-only no-op byte identical", rc == 0 and out == HDR.encode())
a, b, c = "alpha\nbeta\ngamma\n", "alpha\nbeta2\ngamma\n", "alpha\nbeta\ngamma2\n"
rc, out = run(a, b, c)
check("E3c non-yaml adjacent edits conflict like git", rc != 0 and b"<<<<<<<" in out)
a2, b2, c2 = "alpha\nbeta\nmid\ngamma\n", "alpha\nbeta2\nmid\ngamma\n", "alpha\nbeta\nmid\ngamma2\n"
rc, out = run(a2, b2, c2)
check("E3d non-yaml separated edits merge clean", rc == 0 and b"beta2" in out and b"gamma2" in out)

# E4 terminator flips
lf = table((100, "a"), (200, "b"))
crlf = lf.replace("\n", "\r\n")
rc, out = run(lf, crlf, lf)
check("E4a theirs flips file to crlf, ours untouched", rc == 0 and out == crlf.encode())
edited = lf.replace("m_Localized: a", "m_Localized: a edited")
rc, out = run(lf, crlf, edited)
check("E4b flip vs real edit ends loud or merged", rc != 0 or (b"a edited" in out))

# E5 content that looks like conflict markers
marker_doc = table((100, "'<<<<<<< ours in dialogue'"), (200, "b"))
rc, out = run(marker_doc, marker_doc, marker_doc)
check("E5a marker-lookalike content no-op identical", rc == 0 and out == marker_doc.encode())
theirs = marker_doc.replace("m_Localized: b", "m_Localized: b2")
rc, out = run(marker_doc, theirs, marker_doc)
check("E5b marker-lookalike survives a real merge", rc == 0 and b"<<<<<<< ours in dialogue" in out and b"b2" in out)

# E6 reorder plus edit, the classic editor churn
base = table((100, "a"), (200, "b"), (300, "c"))
ours = table((300, "c"), (100, "a"), (200, "b"))
theirs = table((100, "a"), (200, "b edited"), (300, "c"))
rc, out = run(base, theirs, ours)
ok = rc == 0 and b"b edited" in out
ok = ok and out.index(b"m_Id: 300") < out.index(b"m_Id: 100")
check("E6 ours reorders, theirs edits: both honored", ok)

# E7 whole-file rewrite on both sides: disjoint tables union per presence rules
base = table((1, "old"))
ours = table((100, "mine"))
theirs = table((200, "yours"))
rc, out = run(base, theirs, ours)
check("E7 both replaced table: union, base gone", rc == 0 and b"m_Id: 100" in out
      and b"m_Id: 200" in out and b"m_Id: 1\n" not in out)

# E8 multi-document files
two_docs = (HDR + "  m_TableData:\n" + entry(100, "first table") +
            "\n  references:\n    version: 2\n"
            "--- !u!114 &22200000\nMonoBehaviour:\n  m_Name: Second\n  m_TableData:\n" +
            entry(100, "second table uses same id") + "\n  references:\n    version: 2\n")
rc, out = run(two_docs, two_docs, two_docs)
check("E8a same m_Id in two docs no-op identical", rc == 0 and out == two_docs.encode())
theirs = two_docs.replace("first table", "first table edited")
rc, out = run(two_docs, theirs, two_docs)
check("E8b cross-doc duplicate id edit is never silent-wrong",
      (rc == 0 and b"first table edited" in out) or rc != 0)

# E9 anchor extremes
odd = ("%YAML 1.1\n%TAG !u! tag:unity3d.com,2011:\n"
       "--- !u!1 &0\nGameObject:\n  m_Name: zero\n"
       "--- !u!1 &-9223372036854775808\nGameObject:\n  m_Name: min\n"
       "--- !u!1 &9223372036854775807 stripped\nGameObject:\n  m_Name: max\n")
rc, out = run(odd, odd, odd)
check("E9a anchor extremes no-op identical", rc == 0 and out == odd.encode())
theirs = odd.replace("m_Name: min", "m_Name: renamed")
rc, out = run(odd, theirs, odd)
check("E9b negative-anchor doc edit merges", rc == 0 and b"renamed" in out)

# E10 in-place output: OUTPUT path is the same file as LOCAL, like git %A %A
d = tempfile.mkdtemp(prefix="exotic_")
try:
    base_p = os.path.join(d, "base")
    remote_p = os.path.join(d, "remote")
    local_p = os.path.join(d, "local")
    open(base_p, "w", newline="").write(table((100, "a")))
    open(remote_p, "w", newline="").write(table((100, "a"), (200, "new")))
    open(local_p, "w", newline="").write(table((100, "a")))
    r = subprocess.run(DRIVER + [base_p, remote_p, local_p, local_p],
                       capture_output=True, text=True)
    got = open(local_p, "rb").read()
    check("E10 in-place local==output like git", r.returncode == 0 and b"m_Id: 200" in got)
finally:
    shutil.rmtree(d, ignore_errors=True)

print()
if FAILURES:
    print("exotic battery: %d FAILURES: %s" % (len(FAILURES), ", ".join(FAILURES)))
    sys.exit(1)
print("exotic battery: all scenarios pass")
