"""Structural verification: every id-keyed structure must survive a merge
with true 3-way semantics. m_TableData entries are keyed by m_Id.
SerializeReference records are keyed by rid, including the m_SharedEntries
id list that carries the smart-string flags. Whole documents are keyed by
&anchor. The native tool silently violates these; validate_merge is the net."""
from conftest import HDR, RID, asset, entry, refrec, table, uymf


def viols(base, ours, theirs, merged):
    return uymf.validate_merge(base, ours, theirs, merged)


class TestParsers:

    def test_table_entries(self):
        entries, dups = uymf.table_entries(
            asset([entry(100, "a", [RID]), entry(200, "b")], [refrec(RID, [100])]))
        assert set(entries) == {"100", "200"}
        assert entries["100"][1] == frozenset([RID])
        assert entries["200"] == (" b", frozenset(), 1)
        assert not dups

    def test_ids_outside_table_data_do_not_count(self):
        # ids in RefIds payloads or after `references:` are not table entries
        entries, _ = uymf.table_entries(asset([entry(100, "a")], [refrec(RID, [300])]))
        assert set(entries) == {"100"}

    def test_duplicate_ids_reported(self):
        text = asset([entry(100, "a"), entry(100, "b")])
        _, dups = uymf.table_entries(text)
        assert dups == {"100"}

    def test_refid_records(self):
        recs, dups = uymf.refid_records(asset([entry(100, "a", [RID])],
                                              [refrec(RID, [100, 200])]))
        assert set(recs) == {RID}
        assert recs[RID][1] == frozenset(["100", "200"])
        assert "class: SmartFormatTag" in recs[RID][0]
        assert not dups

    def test_crlf_normalized_by_validator(self):
        doc = table((100, "a"))
        assert viols(doc, doc, doc.replace("\n", "\r\n"), doc) == []


class TestDedup:

    def test_identical_duplicate_record_removed(self):
        doc = asset([entry(100, "a", [RID])], [refrec(RID, [100]), refrec(RID, [100])])
        deduped = uymf.dedup_refids(doc)
        assert deduped.count("\n    - rid: %s" % RID) == 1     # the 4-indent RefIds record
        assert deduped == asset([entry(100, "a", [RID])], [refrec(RID, [100])])

    def test_differing_duplicate_introduced_by_merge_is_flagged(self):
        clean = asset([entry(100, "a", [RID])], [refrec(RID, [100])])
        merged = asset([entry(100, "a", [RID])], [refrec(RID, [100]), refrec(RID, [200])])
        assert uymf.dedup_refids(merged).count("\n    - rid: %s" % RID) == 2
        assert any("duplicated with differing content" in v
                   for v in viols(clean, clean, clean, merged))

    def test_inherited_duplicate_is_tolerated(self):
        # corruption already circulating in history must not conflict every
        # future merge; only presence is checked for keys the inputs
        # themselves duplicate
        doc = asset([entry(100, "a", [RID])], [refrec(RID, [100]), refrec(RID, [200])])
        assert viols(doc, doc, doc, doc) == []


class TestEntryRules:

    BASE = asset([entry(100, "victim", [RID]), entry(200, "old")], [refrec(RID, [100])])

    def test_faithful_take_theirs_verifies(self):
        theirs = self.BASE.replace("m_Localized: old", "m_Localized: new")
        assert viols(self.BASE, self.BASE, theirs, theirs) == []

    def test_entry_dropped_from_both_sides(self):
        merged = asset([entry(200, "old")], [refrec(RID, [100])])
        assert any("entry 100 was dropped" in v
                   for v in viols(self.BASE, self.BASE, self.BASE, merged))

    def test_one_sided_delete_is_faithful(self):
        theirs = asset([entry(200, "old")], [refrec(RID, [100])])
        assert viols(self.BASE, self.BASE, theirs, theirs) == []

    def test_deletion_not_applied_is_resurrection(self):
        theirs = asset([entry(200, "old")], [refrec(RID, [100])])
        assert any("deleted on theirs was resurrected" in v
                   for v in viols(self.BASE, self.BASE, theirs, self.BASE))

    def test_edit_vs_delete_conflicts(self):
        ours = self.BASE.replace("m_Localized: victim", "m_Localized: edited")
        theirs = asset([entry(200, "old")], [refrec(RID, [100])])
        assert any("edited on one side but deleted" in v
                   for v in viols(self.BASE, ours, theirs, theirs))

    def test_silent_side_pick_on_both_changed(self):
        ours = self.BASE.replace("m_Localized: old", "m_Localized: OURS")
        theirs = self.BASE.replace("m_Localized: old", "m_Localized: THEIRS")
        assert any("changed differently on both sides" in v
                   for v in viols(self.BASE, ours, theirs, ours))

    def test_revert_to_base_on_both_changed(self):
        ours = self.BASE.replace("m_Localized: old", "m_Localized: OURS")
        theirs = self.BASE.replace("m_Localized: old", "m_Localized: THEIRS")
        assert viols(self.BASE, ours, theirs, self.BASE) != []

    def test_value_swap_matches_neither_side(self):
        merged = self.BASE.replace("m_Localized: victim", "m_Localized: old")
        assert any("entry 100 text matches neither side" in v
                   for v in viols(self.BASE, self.BASE, self.BASE, merged))

    def test_smart_flag_stripped_from_metadata(self):
        # the July symptom: entry text survives but its smart tag is gone
        merged = asset([entry(100, "victim"), entry(200, "old")], [refrec(RID, [100])])
        assert any("entry 100 metadata does not match" in v
                   for v in viols(self.BASE, self.BASE, self.BASE, merged))

    def test_stacked_localized_detected(self):
        merged = self.BASE.replace("    m_Localized: old",
                                   "    m_Localized: old\n    m_Localized: dup")
        assert any("m_Localized fields" in v
                   for v in viols(self.BASE, self.BASE, self.BASE, merged))

    def test_added_entry_must_survive_unaltered(self):
        ours = asset([entry(100, "victim", [RID]), entry(150, "added"), entry(200, "old")],
                     [refrec(RID, [100])])
        assert any("entry 150 added on one side was dropped" in v
                   for v in viols(self.BASE, ours, self.BASE, self.BASE))
        altered = ours.replace("m_Localized: added", "m_Localized: mangled")
        assert any("entry 150 was altered while being added" in v
                   for v in viols(self.BASE, ours, self.BASE, altered))

    def test_metadata_set_merges_both_sides_additions(self):
        # ours adds rid A, theirs adds rid B to the same entry: the merge
        # must keep both
        ours = self.BASE.replace("      m_Items:\n      - rid: " + RID,
                                 "      m_Items:\n      - rid: " + RID + "\n      - rid: 111")
        theirs = self.BASE.replace("      m_Items:\n      - rid: " + RID,
                                   "      m_Items:\n      - rid: " + RID + "\n      - rid: 222")
        good = self.BASE.replace(
            "      m_Items:\n      - rid: " + RID,
            "      m_Items:\n      - rid: " + RID + "\n      - rid: 111\n      - rid: 222")
        assert [v for v in viols(self.BASE, ours, theirs, good)
                if "metadata" in v] == []
        assert any("entry 100 metadata does not match" in v
                   for v in viols(self.BASE, ours, theirs, ours))


class TestReferenceRules:

    BASE = asset([entry(100, "'{smart}'", [RID]), entry(200, "plain")],
                 [refrec(RID, [100])])

    def test_record_dropped(self):
        merged = asset([entry(100, "'{smart}'", [RID]), entry(200, "plain")])
        got = viols(self.BASE, self.BASE, self.BASE, merged)
        assert any("reference record %s was dropped" % RID in v for v in got)
        assert any("references rid %s which has no record" % RID in v for v in got)

    def test_shared_id_dropped_from_record(self):
        base = asset([entry(100, "'{a}'", [RID]), entry(200, "'{b}'", [RID])],
                     [refrec(RID, [100, 200])])
        merged = asset([entry(100, "'{a}'", [RID]), entry(200, "'{b}'", [RID])],
                       [refrec(RID, [200])])
        assert any("reference %s entry ids does not match" % RID in v
                   for v in viols(base, base, base, merged))

    def test_shared_id_additions_merge_as_set(self):
        ours = self.BASE.replace("        - id: 100", "        - id: 100\n        - id: 111")
        theirs = self.BASE.replace("        - id: 100", "        - id: 100\n        - id: 222")
        good = self.BASE.replace("        - id: 100",
                                 "        - id: 100\n        - id: 111\n        - id: 222")
        assert [v for v in viols(self.BASE, ours, theirs, good) if "entry ids" in v] == []

    def test_payload_change_verified(self):
        theirs = self.BASE.replace("        m_Entries: ", "        m_Entries: changed")
        assert viols(self.BASE, self.BASE, theirs, theirs) == []
        assert any("reference %s payload lost theirs' change" % RID in v
                   for v in viols(self.BASE, self.BASE, theirs, self.BASE))


class TestDocumentRules:

    PREFAB = ("%YAML 1.1\n%TAG !u! tag:unity3d.com,2011:\n"
              "--- !u!1 &100\nGameObject:\n  m_Name: Root\n"
              "--- !u!114 &200\nMonoBehaviour:\n  m_Enabled: 1\n")

    def test_document_dropped(self):
        merged = self.PREFAB.replace("--- !u!114 &200\nMonoBehaviour:\n  m_Enabled: 1\n", "")
        assert any("document &200 was dropped" in v
                   for v in viols(self.PREFAB, self.PREFAB, self.PREFAB, merged))

    def test_document_added_on_one_side_dropped(self):
        ours = self.PREFAB + "--- !u!114 &300\nMonoBehaviour:\n  m_New: 1\n"
        assert any("document &300 added on one side was dropped" in v
                   for v in viols(self.PREFAB, ours, self.PREFAB, self.PREFAB))
        assert viols(self.PREFAB, ours, self.PREFAB, ours) == []
