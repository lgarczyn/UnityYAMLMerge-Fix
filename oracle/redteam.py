"""Red-team battery: fabricated native-tool misbehaviors that must never
pass silently. Reports caught, healed, or SILENT per scenario.

usage: redteam.py <driver-cmd...>   default: the Python reference
A driver that replaces the native tool ignores UNITY_YAML_MERGE; the env
var is still set so wrapper-style drivers can be tested too."""
import os
import subprocess
import sys
import tempfile

import pathlib
ROOT = pathlib.Path(__file__).resolve().parent.parent
DRIVER = sys.argv[1:] or ["python3", str(ROOT / "unityyamlmerge_fix.py")]

HDR = ("%YAML 1.1\n%TAG !u! tag:unity3d.com,2011:\n--- !u!114 &11400000\nMonoBehaviour:\n"
       "  m_Name: Table_en\n")


def entry(eid, text, rids=()):
    md = "      m_Items: []" if not rids else \
         "      m_Items:\n" + "\n".join("      - rid: %d" % r for r in rids)
    return "  - m_Id: %d\n    m_Localized: %s\n    m_Metadata:\n%s" % (eid, text, md)


def refrec(rid, ids):
    return ("    - rid: %d\n"
            "      type: {class: SmartFormatTag, ns: UnityEngine.Localization.Metadata, asm: Unity.Localization}\n"
            "      data:\n"
            "        m_Entries: \n"
            "        m_SharedEntries:\n" % rid
            + "\n".join("        - id: %d" % i for i in ids))


def table(entries, refs):
    return (HDR + "  m_TableData:\n" + "\n".join(entries)
            + "\n  references:\n    version: 2\n    RefIds:\n" + "\n".join(refs) + "\n")


RID = 842043826615615503
BASE = table(
    [entry(100, "plain string"), entry(200, "'{smart} string'", [RID]), entry(300, "third")],
    [refrec(RID, [200, 300])])
# theirs rewrites entry 300's text; ours == base
THEIRS = BASE.replace("m_Localized: third", "m_Localized: third rewritten")
OURS = BASE

PREFAB = ("%YAML 1.1\n%TAG !u! tag:unity3d.com,2011:\n"
          "--- !u!1 &100\nGameObject:\n  m_Name: Root\n"
          "--- !u!114 &200\nMonoBehaviour:\n  m_Script: {fileID: 11500000}\n"
          "--- !u!114 &300\nMonoBehaviour:\n  m_Enabled: 1\n")
PREFAB_THEIRS = PREFAB.replace("m_Name: Root", "m_Name: RootRenamed")

FAKE = """#!/usr/bin/env python3
import re, shutil, sys
base, remote, local, out = sys.argv[-4:]
text = open(remote).read()
{body}
open(out, 'w').write(text)
"""

SCENARIOS = [
    ("S1 drop whole m_Id entry",
     "text = re.sub(r'  - m_Id: 100\\n(    .*\\n)*', '', text)",
     lambda out: "m_Id: 100" in out, (BASE, THEIRS, OURS)),
    ("S2 drop RefIds record (SerializeReference)",
     "text = re.sub(r'(?m)^    - rid: 842043826615615503\\n(      .*\\n)*', '', text)",
     lambda out: "- rid: 842043826615615503" in out.split("RefIds:")[-1], (BASE, THEIRS, OURS)),
    ("S3 strip smart-flag rid from entry metadata",
     "text = text.replace('    m_Metadata:\\n      m_Items:\\n      - rid: 842043826615615503',"
     " '    m_Metadata:\\n      m_Items: []')",
     lambda out: "      - rid: 842043826615615503" in out, (BASE, THEIRS, OURS)),
    ("S4 drop one id from m_SharedEntries",
     "text = text.replace('        - id: 200\\n', '')",
     lambda out: "- id: 200" in out, (BASE, THEIRS, OURS)),
    ("S5 duplicate the RefIds record",
     "text = text.replace('  references:', '', 1) if False else text\n"
     "rec = re.search(r'    - rid: 842043826615615503\\n(?:      .*\\n|        .*\\n)*', text).group(0)\n"
     "text = text.replace(rec, rec + rec)",
     lambda out: out.count("- rid: 842043826615615503") -
                 out.split("RefIds:")[0].count("- rid: 842043826615615503") == 1,
     (BASE, THEIRS, OURS)),
    ("S6 cross-entry value swap",
     "text = text.replace('m_Localized: plain string', 'm_Localized: third rewritten')",
     lambda out: "m_Localized: plain string" in out, (BASE, THEIRS, OURS)),
    ("S7 silent side-pick on both-changed entry",
     "shutil.copyfile(local, out); text = open(out).read()",
     lambda out: "OURS-EDIT" in out and "THEIRS-EDIT" in out,
     (BASE,
      BASE.replace("m_Localized: third", "m_Localized: THEIRS-EDIT"),
      BASE.replace("m_Localized: third", "m_Localized: OURS-EDIT"))),
    ("S8 drop whole document from prefab",
     "text = re.sub(r'--- !u!114 &300\\nMonoBehaviour:\\n  m_Enabled: 1\\n', '', text)",
     lambda out: "&300" in out, (PREFAB, PREFAB_THEIRS, PREFAB)),
    ("S9 revert both-changed entry to base",
     "text = text.replace('m_Localized: THEIRS-EDIT', 'm_Localized: third')",
     lambda out: "third rewritten" in out or "OURS-EDIT" in out or "<<<<<<<" in out,
     (BASE,
      BASE.replace("m_Localized: third", "m_Localized: THEIRS-EDIT"),
      BASE.replace("m_Localized: third", "m_Localized: OURS-EDIT"))),
]

silent = 0
for name, body, intact, files in SCENARIOS:
    d = tempfile.mkdtemp(prefix="redteam_")
    tool = os.path.join(d, "tool")
    open(tool, "w").write(FAKE.format(body=body))
    os.chmod(tool, 0o755)
    paths = []
    for fname, text in zip(("base", "remote", "local", "out"), files + (files[2],)):
        p = os.path.join(d, fname)
        open(p, "w").write(text)
        paths.append(p)
    r = subprocess.run(DRIVER + paths, capture_output=True, text=True,
                       env=dict(os.environ, UNITY_YAML_MERGE=tool))
    out = open(paths[3]).read()
    ok = intact(out)
    if r.returncode == 0 and not ok:
        verdict = "SILENT LOSS"
        silent += 1
    elif r.returncode == 0:
        verdict = "healed"
    else:
        verdict = "caught (rc=%d)" % r.returncode
    print("%-45s %s" % (name, verdict))
print("\n%d/%d scenarios pass silently" % (silent, len(SCENARIOS)))
