"""The driver script's fallback chain. A failed merge driver is how main's
localization work got silently reverted: git leaves OURS with no markers on
driver failure and designers commit it. Whatever runtime is missing, the
script must still run a real merge."""
import os
import shutil
import subprocess
import sys

import pytest

from conftest import ROOT, mono_and_exe, table

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX sh test")

SCRIPT = str(ROOT / "yamlmerge-driver.sh")

# a stand-in native tool: sh only, so it also runs on the PATH-stripped
# fallback test
FAKE_NATIVE = """#!/bin/sh
shift $(($# - 4))
cp "$2" "$4"
"""


def editor_layout(tmp_path, with_mono):
    """A fake <Editor>/Data tree: the tool plus optionally mono."""
    tools = tmp_path / "Data" / "Tools"
    tools.mkdir(parents=True)
    tool = tools / "UnityYAMLMerge"
    tool.write_text(FAKE_NATIVE)
    tool.chmod(0o755)
    if with_mono:
        mono, _ = mono_and_exe()
        bindir = tmp_path / "Data" / "MonoBleedingEdge" / "bin"
        bindir.mkdir(parents=True)
        (bindir / "mono").symlink_to(mono)
    return str(tool)


def run_script(tool, workdir, base, theirs, ours, path=None):
    workdir.mkdir()
    paths = []
    for name, text in zip(("base", "remote", "local", "out"), (base, theirs, ours, ours)):
        p = workdir / name
        p.write_bytes(text.encode())
        paths.append(str(p))
    env = dict(os.environ)
    if path is not None:
        env["PATH"] = path
    r = subprocess.run(["sh", SCRIPT, tool] + paths, capture_output=True, env=env)
    return r.returncode, (workdir / "out").read_bytes().decode()


BASE = OURS = table((100, "a"), (200, "old"))
THEIRS = table((100, "a"), (200, "new"))


class TestFallbackChain:

    def test_mono_path_runs_csharp_wrapper(self, tmp_path):
        if not mono_and_exe()[0]:
            pytest.skip("uymf.exe or Unity mono not available")
        tool = editor_layout(tmp_path, with_mono=True)
        rc, out = run_script(tool, tmp_path / "w", BASE, THEIRS, OURS)
        assert rc == 0
        assert out == THEIRS

    def test_no_mono_falls_back_to_python(self, tmp_path):
        tool = editor_layout(tmp_path, with_mono=False)
        rc, out = run_script(tool, tmp_path / "w", BASE, THEIRS, OURS)
        assert rc == 0
        assert out == THEIRS

    def test_no_runtime_at_all_still_merges_natively(self, tmp_path):
        # neither mono nor python3 available: the script must exec the
        # native tool directly rather than fail, because a failing driver
        # becomes marker-less keep-ours
        tool = editor_layout(tmp_path, with_mono=False)
        bindir = tmp_path / "bin"
        bindir.mkdir()
        for b in ("sh", "dirname", "cp"):
            (bindir / b).symlink_to(shutil.which(b))
        rc, out = run_script(tool, tmp_path / "w", BASE, THEIRS, OURS, path=str(bindir))
        assert rc == 0
        assert out == THEIRS
