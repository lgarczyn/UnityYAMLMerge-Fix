#!/usr/bin/env python3
"""Model-based random-ops testing. Build a random string-table asset, apply
independent random edit scripts to ours and theirs, predict the exact merge
outcome from the SPEC rules alone, then require uymerge to produce it:
predicted conflicts must exit non-zero, predicted clean merges must exit
zero with exactly the predicted entries, texts, rid sets and record ids.

usage: modelcheck.py [iterations] [driver...]   default 2000 iterations
"""
import copy
import importlib.util
import os
import pathlib
import random
import shutil
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
ITER = int(sys.argv[1]) if len(sys.argv) > 1 else 2000
DRIVER = sys.argv[2:] or [str(ROOT / "target/release/uymerge")]

spec = importlib.util.spec_from_file_location("uymf", ROOT / "unityyamlmerge_fix.py")
uymf = importlib.util.module_from_spec(spec)
spec.loader.exec_module(uymf)

HDR = ("%YAML 1.1\n%TAG !u! tag:unity3d.com,2011:\n--- !u!114 &11400000\n"
       "MonoBehaviour:\n  m_Name: Table_en\n")

WORDS = ["alpha", "beta", "gamma", "delta", "omega", "kappa", "sigma"]


def render(model):
    # model: {"entries": [(id, text, [rids])...], "records": [(rid, [ids])...]}
    out = [HDR + "  m_TableData:"]
    for eid, text, rids in model["entries"]:
        md = ("      m_Items: []" if not rids else
              "      m_Items:\n" + "\n".join("      - rid: %s" % r for r in rids))
        out.append("  - m_Id: %s\n    m_Localized: %s\n    m_Metadata:\n%s"
                   % (eid, text, md))
    out.append("  references:\n    version: 2")
    if model["records"]:
        out[-1] += "\n    RefIds:"
        for rid, ids in model["records"]:
            out.append("    - rid: %s\n      type: {class: SmartFormatTag, "
                       "ns: N, asm: A}\n      data:\n        m_Entries: \n"
                       "        m_SharedEntries:"
                       % rid + "".join("\n        - id: %s" % i for i in ids))
    return "\n".join(out) + "\n"


def apply_ops(rng, model, other_new_ids):
    """Mutate a deep copy with a few random ops; return (model, oplog)."""
    m = copy.deepcopy(model)
    ops = []
    for _ in range(rng.randint(1, 4)):
        kind = rng.choice(["edit", "add", "del", "rid_add", "rid_del",
                           "shared_add", "shared_del", "reorder"])
        if kind == "edit" and m["entries"]:
            i = rng.randrange(len(m["entries"]))
            eid, _t, rids = m["entries"][i]
            new = rng.choice(WORDS) + str(rng.randint(0, 999))
            m["entries"][i] = (eid, new, rids)
            ops.append(("edit", eid, new))
        elif kind == "add":
            eid = str(rng.randint(10**6, 10**9))
            while any(e[0] == eid for e in m["entries"]) or eid in other_new_ids:
                eid = str(rng.randint(10**6, 10**9))
            other_new_ids.add(eid)
            text = rng.choice(WORDS)
            pos = rng.randint(0, len(m["entries"]))
            m["entries"].insert(pos, (eid, text, []))
            ops.append(("add", eid, text))
        elif kind == "del" and m["entries"]:
            i = rng.randrange(len(m["entries"]))
            ops.append(("del", m["entries"][i][0], None))
            del m["entries"][i]
        elif kind == "rid_add" and m["entries"] and m["records"]:
            i = rng.randrange(len(m["entries"]))
            eid, t, rids = m["entries"][i]
            rid = rng.choice(m["records"])[0]
            if rid not in rids:
                m["entries"][i] = (eid, t, rids + [rid])
                ops.append(("rid_add", eid, rid))
        elif kind == "rid_del" and m["entries"]:
            i = rng.randrange(len(m["entries"]))
            eid, t, rids = m["entries"][i]
            if rids:
                r = rng.choice(rids)
                m["entries"][i] = (eid, t, [x for x in rids if x != r])
                ops.append(("rid_del", eid, r))
        elif kind == "shared_add" and m["records"]:
            i = rng.randrange(len(m["records"]))
            rid, ids = m["records"][i]
            new = str(rng.randint(10**6, 10**9))
            m["records"][i] = (rid, ids + [new])
            ops.append(("shared_add", rid, new))
        elif kind == "shared_del" and m["records"]:
            i = rng.randrange(len(m["records"]))
            rid, ids = m["records"][i]
            if ids:
                x = rng.choice(ids)
                m["records"][i] = (rid, [y for y in ids if y != x])
                ops.append(("shared_del", rid, x))
        elif kind == "reorder" and len(m["entries"]) > 1:
            rng.shuffle(m["entries"])
            ops.append(("reorder", None, None))
    return m, ops


def as_maps(model):
    entries = {e[0]: (e[1], frozenset(e[2])) for e in model["entries"]}
    records = {r[0]: frozenset(r[1]) for r in model["records"]}
    return entries, records


def predict(b, o, t):
    """Expected (conflict, entries, records) per SPEC 4. None fields on
    conflict since content is then unspecified."""
    eb, rb = as_maps(b)
    eo, ro = as_maps(o)
    et, rt = as_maps(t)
    conflict = False
    entries = {}
    for k in set(eb) | set(eo) | set(et):
        in_o, in_t = k in eo, k in et
        if in_o and in_t:
            bt = eb.get(k, (None, frozenset()))
            # text scalar rule
            if eo[k][0] == et[k][0]:
                text = eo[k][0]
            elif bt[0] == eo[k][0]:
                text = et[k][0]
            elif bt[0] == et[k][0]:
                text = eo[k][0]
            else:
                conflict = True
                text = None
            # rid set rule
            base_r = bt[1]
            rids = (base_r & eo[k][1] & et[k][1]) | (eo[k][1] - base_r) | (et[k][1] - base_r)
            entries[k] = (text, rids)
        elif in_o or in_t:
            side = eo if in_o else et
            if k not in eb:
                entries[k] = side[k]
            elif side[k] == eb[k]:
                pass  # deletion applies
            else:
                conflict = True
                entries[k] = side[k]
    records = {}
    for k in set(rb) | set(ro) | set(rt):
        in_o, in_t = k in ro, k in rt
        if in_o and in_t:
            base_r = rb.get(k, frozenset())
            records[k] = (base_r & ro[k] & rt[k]) | (ro[k] - base_r) | (rt[k] - base_r)
        elif in_o or in_t:
            side = ro if in_o else rt
            if k not in rb:
                records[k] = side[k]
            elif side[k] == rb[k]:
                pass
            else:
                conflict = True
                records[k] = side[k]
    return conflict, entries, records


def parse_output(text):
    u = uymf.reserialize(text, uymf.INF, uymf.INF, fix_empty=False)
    e, _ = uymf.table_entries(u)
    r, _ = uymf.refid_records(u)
    entries = {k: (v[0].strip(), frozenset(v[1])) for k, v in e.items()}
    records = {k: frozenset(v[1]) for k, v in r.items()}
    return entries, records


work = tempfile.mkdtemp(prefix="model_")
fails = 0
for it in range(ITER):
    rng = random.Random(1000 + it)
    base = {"entries": [(str(rng.randint(10**6, 10**9)), rng.choice(WORDS), [])
                        for _ in range(rng.randint(1, 6))],
            "records": [(str(rng.randint(10**12, 10**15)),
                         [str(rng.randint(10**6, 10**9))
                          for _ in range(rng.randint(0, 3))])
                        for _ in range(rng.randint(0, 2))]}
    seen = {e[0] for e in base["entries"]}
    if len(seen) != len(base["entries"]):
        continue
    new_ids = set()
    ours, _ = apply_ops(rng, base, new_ids)
    theirs, _ = apply_ops(rng, base, new_ids)
    conflict, exp_e, exp_r = predict(base, ours, theirs)

    paths = []
    for name, mdl in (("base", base), ("remote", theirs), ("local", ours)):
        p = os.path.join(work, name)
        open(p, "w", newline="").write(render(mdl))
        paths.append(p)
    out = os.path.join(work, "out")
    shutil.copyfile(paths[2], out)
    r = subprocess.run(DRIVER + paths + [out], capture_output=True, text=True)

    if conflict:
        if r.returncode == 0:
            fails += 1
            print("iter %d: predicted conflict, uymerge exited 0" % it)
    else:
        if r.returncode != 0:
            fails += 1
            print("iter %d: predicted clean, uymerge exited %d" % (it, r.returncode))
        else:
            got_e, got_r = parse_output(open(out, newline="").read())
            exp_e_cmp = {k: (v[0].strip() if v[0] else v[0], v[1])
                         for k, v in exp_e.items()}
            if got_e != exp_e_cmp or got_r != exp_r:
                fails += 1
                print("iter %d: content mismatch" % it)
                for k in set(got_e) | set(exp_e_cmp):
                    if got_e.get(k) != exp_e_cmp.get(k):
                        print("  entry %s exp=%s got=%s"
                              % (k, exp_e_cmp.get(k), got_e.get(k)))
                for k in set(got_r) | set(exp_r):
                    if got_r.get(k) != exp_r.get(k):
                        print("  record %s exp=%s got=%s"
                              % (k, exp_r.get(k), got_r.get(k)))
    if fails >= 10:
        print("too many failures, stopping early")
        break
shutil.rmtree(work, ignore_errors=True)
print("modelcheck: %d iterations, %d failures" % (it + 1, fails))
sys.exit(1 if fails else 0)
