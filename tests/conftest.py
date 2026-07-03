import glob
import importlib.util
import os
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent

_spec = importlib.util.spec_from_file_location("uymf", ROOT / "unityyamlmerge_fix.py")
uymf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(uymf)

HDR = (
    "%YAML 1.1\n"
    "%TAG !u! tag:unity3d.com,2011:\n"
    "--- !u!114 &11400000\n"
    "MonoBehaviour:\n"
    "  m_Name: Table_en\n"
)


RID = "842043826615615503"


def entry(eid, text, rids=()):
    md = ("      m_Items: []" if not rids else
          "      m_Items:\n" + "\n".join("      - rid: %s" % r for r in rids))
    return "  - m_Id: %s\n    m_Localized: %s\n    m_Metadata:\n%s" % (eid, text, md)


def refrec(rid, ids):
    return ("    - rid: %s\n"
            "      type: {class: SmartFormatTag, ns: UnityEngine.Localization.Metadata,"
            " asm: Unity.Localization}\n"
            "      data:\n"
            "        m_Entries: \n"
            "        m_SharedEntries:\n" % rid
            + "\n".join("        - id: %s" % i for i in ids))


def asset(entries=(), refs=()):
    """A localization string-table asset from pre-built entry/refrec blocks."""
    text = HDR + "  m_TableData:"
    for e in entries:
        text += "\n" + e
    text += "\n  references:\n    version: 2"
    if refs:
        text += "\n    RefIds:"
        for r in refs:
            text += "\n" + r
    return text + "\n"


def table(*pairs):
    """A minimal string-table asset with the given (id, text) entries."""
    return asset([entry(eid, text) for eid, text in pairs])


# A fake native tool lets tests force the exact UnityYAMLMerge behaviors the
# bug report documents, like silently dropping an m_Id record, without
# depending on reproducing them with the real binary on synthetic files.
FAKE_TOOL = """#!/usr/bin/env python3
import re, shutil, sys
base, remote, local, out = sys.argv[-4:]
{body}
"""


@pytest.fixture
def fake_tool(tmp_path):
    def make(body):
        p = tmp_path / "faketool"
        p.write_text(FAKE_TOOL.format(body=body))
        p.chmod(0o755)
        return str(p)
    return make


TAKE_THEIRS = "shutil.copyfile(remote, out)"

# copy theirs but silently eat one whole m_Id record, exit 0 -- the native bug
def drop_entry(eid):
    return ("text = open(remote).read()\n"
            "text = re.sub(r'  - m_Id: %s\\n(    .*\\n)*', '', text)\n"
            "open(out, 'w').write(text)" % eid)


def mono_and_exe():
    exe = ROOT / "uymf.exe"
    monos = sorted(glob.glob(os.path.expanduser(
        "~/Unity/Hub/Editor/*/Editor/Data/MonoBleedingEdge/bin/mono")))
    if not exe.exists() or not monos:
        return None, None
    return monos[-1], str(exe)


@pytest.fixture
def csharp():
    mono, exe = mono_and_exe()
    if not mono:
        pytest.skip("uymf.exe or Unity mono not available")
    return mono, exe
