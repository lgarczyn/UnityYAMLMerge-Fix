"""End-to-end driver behavior around a fake native tool that reproduces the
failure modes from the bug report: a clean merge, a silent m_Id drop that is
recoverable by text merge, one that must fail hard, and a real conflict."""
import pytest

from conftest import RID, TAKE_THEIRS, asset, drop_entry, entry, refrec, table, uymf


def run_merge(monkeypatch, tmp_path, tool, base, theirs, ours):
    monkeypatch.setenv("UNITY_YAML_MERGE", tool)
    paths = {}
    for name, text in (("base", base), ("remote", theirs), ("local", ours), ("out", ours)):
        p = tmp_path / name
        p.write_bytes(text.encode())
        paths[name] = str(p)
    rc = uymf.merge(paths["base"], paths["remote"], paths["local"], paths["out"])
    return rc, (tmp_path / "out").read_bytes().decode()


class TestCleanMerge:

    def test_ours_unchanged_takes_theirs(self, monkeypatch, tmp_path, fake_tool):
        base = ours = table((100, "hello"), (200, "world"))
        theirs = table((100, "hello"), (200, "world rewritten"))
        rc, out = run_merge(monkeypatch, tmp_path, fake_tool(TAKE_THEIRS), base, theirs, ours)
        assert rc == 0
        assert out == theirs

    def test_injected_empty_flow_values_are_stripped(self, monkeypatch, tmp_path, fake_tool):
        # the native merge turns `{class: , ns: }` into `{class: '', ns: ''}`.
        # The editor writes them bare, so the rewrap must strip the injection.
        base = ours = table((100, "a"))
        theirs = base.replace("  m_Name: Table_en",
                              "  m_Name: Table_en\n  type: {class: X, ns: , asm: }")
        inject = TAKE_THEIRS + "\nopen(out, 'w').write(open(out).read().replace('ns: ,', \"ns: '',\"))"
        rc, out = run_merge(monkeypatch, tmp_path, fake_tool(inject), base, theirs, ours)
        assert rc == 0
        assert "''" not in out
        assert "type: {class: X, ns: , asm: }" in out

    def test_crlf_input_stays_crlf(self, monkeypatch, tmp_path, fake_tool):
        base = ours = table((100, "a")).replace("\n", "\r\n")
        theirs = table((100, "a rewritten")).replace("\n", "\r\n")
        rc, out = run_merge(monkeypatch, tmp_path, fake_tool(TAKE_THEIRS), base, theirs, ours)
        assert rc == 0
        assert out == theirs


class TestDroppedEntryGuard:

    def test_recoverable_drop_is_healed_by_text_merge(self, monkeypatch, tmp_path,
                                                      fake_tool, capfd):
        # ours == base, theirs rewrites 200, the tool eats 100. The text
        # merge of the unwrapped inputs is clean and valid, so it self-heals.
        base = ours = table((100, "victim"), (200, "old"))
        theirs = table((100, "victim"), (200, "new"))
        rc, out = run_merge(monkeypatch, tmp_path, fake_tool(drop_entry(100)),
                            base, theirs, ours)
        assert rc == 0
        assert "m_Id: 100" in out and "m_Localized: new" in out
        assert "recovered by text merge" in capfd.readouterr().err

    def test_unrecoverable_drop_fails_with_whole_file_conflict(self, monkeypatch, tmp_path,
                                                               fake_tool, capfd):
        # both sides added a different entry under the SAME id far apart.
        # The text merge is clean but contains a duplicate id, so it must be
        # refused and the working file must scream. A marker-less lossy file
        # must never be left behind.
        base = table((100, "victim"), (300, "keep"))
        ours = table((100, "victim"), (200, "from ours"), (300, "keep"))
        theirs = table((100, "victim"), (300, "keep"), (200, "from theirs"))
        rc, out = run_merge(monkeypatch, tmp_path, fake_tool(drop_entry(100)),
                            base, theirs, ours)
        assert rc != 0
        assert out.startswith("<<<<<<< ours\n") and out.endswith(">>>>>>> theirs\n")
        assert "resolve by hand" in capfd.readouterr().err

    def test_conflicting_drop_leaves_markers(self, monkeypatch, tmp_path, fake_tool, capfd):
        # both sides rewrote 200 differently and the tool also ate 100. The
        # text merge conflicts, so the driver exits non-zero with markers.
        base = table((100, "victim"), (200, "old"))
        ours = table((100, "victim"), (200, "ours version"))
        theirs = table((100, "victim"), (200, "theirs version"))
        rc, out = run_merge(monkeypatch, tmp_path, fake_tool(drop_entry(100)),
                            base, theirs, ours)
        assert rc != 0
        assert "<<<<<<<" in out and ">>>>>>>" in out
        assert "resolve by hand" in capfd.readouterr().err

    def test_one_sided_delete_does_not_fire(self, monkeypatch, tmp_path, fake_tool, capfd):
        # theirs deleted 100 on purpose; taking theirs is correct, not a drop
        base = ours = table((100, "gone"), (200, "b"))
        theirs = table((200, "b"))
        rc, out = run_merge(monkeypatch, tmp_path, fake_tool(TAKE_THEIRS), base, theirs, ours)
        assert rc == 0
        assert "m_Id: 100" not in out
        assert capfd.readouterr().err == ""


class TestSerializeReference:

    def test_stripped_smart_flag_is_healed(self, monkeypatch, tmp_path, fake_tool, capfd):
        # the July symptom end to end: the tool keeps the entry but strips
        # its smart tag
        base = ours = theirs = asset([entry(100, "'{smart}'", [RID]), entry(200, "x")],
                                     [refrec(RID, [100])])
        strip = ("text = open(remote).read().replace("
                 "'      m_Items:\\n      - rid: %s', '      m_Items: []')\n"
                 "open(out, 'w').write(text)" % RID)
        rc, out = run_merge(monkeypatch, tmp_path, fake_tool(strip), base, theirs, ours)
        assert rc == 0
        assert "      - rid: %s" % RID in out
        assert "recovered by text merge" in capfd.readouterr().err

    def test_dropped_shared_id_is_healed(self, monkeypatch, tmp_path, fake_tool, capfd):
        base = ours = theirs = asset([entry(100, "'{a}'", [RID]), entry(200, "'{b}'", [RID])],
                                     [refrec(RID, [100, 200])])
        drop_id = ("text = open(remote).read().replace('        - id: 100\\n', '')\n"
                   "open(out, 'w').write(text)")
        rc, out = run_merge(monkeypatch, tmp_path, fake_tool(drop_id), base, theirs, ours)
        assert rc == 0
        assert "        - id: 100" in out
        assert "recovered by text merge" in capfd.readouterr().err

    def test_duplicated_record_is_deduped_silently(self, monkeypatch, tmp_path,
                                                   fake_tool, capfd):
        base = ours = theirs = asset([entry(100, "'{smart}'", [RID])], [refrec(RID, [100])])
        dup = ("import re\n"
               "text = open(remote).read()\n"
               "rec = re.search(r'(?m)^    - rid: %s\\n(      .*\\n?)*', text).group(0)\n"
               "text = text.replace(rec, rec.rstrip('\\n') + '\\n' + rec.rstrip('\\n') + '\\n')\n"
               "open(out, 'w').write(text)" % RID)
        rc, out = run_merge(monkeypatch, tmp_path, fake_tool(dup), base, theirs, ours)
        assert rc == 0
        assert out.count("- rid: %s" % RID) - out.split("RefIds:")[0].count(
            "- rid: %s" % RID) == 1
        assert capfd.readouterr().err == ""

    def test_side_pick_on_both_changed_becomes_conflict(self, monkeypatch, tmp_path,
                                                        fake_tool, capfd):
        # both sides rewrote the same entry and the tool silently keeps ours
        # with exit 0. Verification must turn that into a real conflict.
        base = table((100, "original"))
        ours = table((100, "OURS"))
        theirs = table((100, "THEIRS"))
        rc, out = run_merge(monkeypatch, tmp_path, fake_tool("shutil.copyfile(local, out)"),
                            base, theirs, ours)
        assert rc != 0
        assert "<<<<<<<" in out and "OURS" in out and "THEIRS" in out
        assert "resolve by hand" in capfd.readouterr().err


class TestToolFailure:

    def test_tool_error_without_output_propagates_failure(self, monkeypatch, tmp_path,
                                                          fake_tool):
        base = ours = theirs = table((100, "a"))
        rc, out = run_merge(monkeypatch, tmp_path,
                            fake_tool("import os\nos.remove(out)\nsys.exit(3)"),
                            base, theirs, ours)
        assert rc == 3

    def test_missing_tool_fails(self, monkeypatch, tmp_path):
        monkeypatch.setenv("UNITY_YAML_MERGE", str(tmp_path / "nope"))
        monkeypatch.setattr(uymf.glob, "glob", lambda *_: [])
        with pytest.raises(SystemExit):
            uymf.merge("a", "b", "c", "d")
