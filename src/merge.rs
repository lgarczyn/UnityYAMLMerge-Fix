//! Keyed record merge, the structural core. SPEC section 4. Packet P6.
//! Builds the merged m_TableData and references/RefIds record sets from base,
//! ours and theirs by presence, content and reassembly rules.
//!
//! The reference is a verifier, validate_merge, not a constructor. This module
//! inverts its rules. Presence per SPEC 4.1, duplicates per 4.4 and record
//! order per 4.5 are explicit constructors here. A record present and changed
//! on both sides has its raw block merged line by line through the P5 diff3
//! engine, which realizes the scalar rule 4.2 and set rule 4.3 for disjoint
//! changes and emits markers otherwise. That mirrors the reference fallback
//! text_merge, which also line-merges, and reuses raw bytes so output stays
//! editor faithful. The P8 verifier is the backstop for the structural rules.
//!
//! Everything works on lines split on '\n' with no terminators, the same space
//! as the model parsers, so P7 can splice a merged section back into a document
//! by lines. Blocks are fed to diff3 with a '\n' reattached per line and the
//! rendered text is split back, which keeps line identity and bytes intact.

use std::collections::{BTreeMap, BTreeSet};

use crate::diff3;
use crate::model::{self, Span};

/// A merged record section body: the record block lines in output order and
/// whether any per-record conflict marker was emitted.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct SectionMerge {
    pub lines: Vec<String>,
    pub conflict: bool,
}

/// Merge the m_TableData record set across three document texts.
pub fn merge_table(base: &str, ours: &str, theirs: &str) -> SectionMerge {
    merge_sections(
        &table_section(base),
        &table_section(ours),
        &table_section(theirs),
    )
}

/// Merge the references/RefIds record set across three document texts.
pub fn merge_refids(base: &str, ours: &str, theirs: &str) -> SectionMerge {
    merge_sections(
        &refids_section(base),
        &refids_section(ours),
        &refids_section(theirs),
    )
}

// A keyed record section reduced to what the merge needs: key order, the keys
// duplicated in the input and every key's occurrence blocks as raw line lists.
struct Section {
    order: Vec<String>,
    dups: BTreeSet<String>,
    blocks: BTreeMap<String, Vec<Vec<String>>>,
}

impl Section {
    fn has(&self, k: &str) -> bool {
        self.blocks.contains_key(k)
    }

    // First occurrence block. Callers gate this on has, so the key is present.
    fn first(&self, k: &str) -> &[String] {
        &self.blocks[k][0]
    }
}

fn table_section(text: &str) -> Section {
    let td = model::table_entries(text);
    let spans = td.entries.into_iter().map(|(k, e)| (k, e.spans)).collect();
    build_section(text, td.order, td.dups, spans)
}

fn refids_section(text: &str) -> Section {
    let rd = model::refid_records(text);
    let spans = rd.records.into_iter().map(|(k, r)| (k, r.spans)).collect();
    build_section(text, rd.order, rd.dups, spans)
}

fn build_section(
    text: &str,
    order: Vec<String>,
    dups: BTreeSet<String>,
    spans: BTreeMap<String, Vec<Span>>,
) -> Section {
    let lines: Vec<&str> = text.split('\n').collect();
    let mut blocks: BTreeMap<String, Vec<Vec<String>>> = BTreeMap::new();
    for (k, occ) in spans {
        let occs = occ
            .iter()
            .map(|s| {
                lines[s.start..s.end]
                    .iter()
                    .map(|l| (*l).to_string())
                    .collect()
            })
            .collect();
        blocks.insert(k, occs);
    }
    Section {
        order,
        dups,
        blocks,
    }
}

// Per-key merge outcome: whether the key survives, whether it conflicted, and
// the block lines to emit, possibly several for carried-through duplicates.
struct Resolution {
    present: bool,
    conflict: bool,
    blocks: Vec<Vec<String>>,
}

fn merge_sections(b: &Section, o: &Section, t: &Section) -> SectionMerge {
    // Keys duplicated in any input are inherited corruption per SPEC 4.4.
    let mut skip = b.dups.clone();
    skip.extend(o.dups.iter().cloned());
    skip.extend(t.dups.iter().cloned());

    let mut keys: BTreeSet<String> = BTreeSet::new();
    keys.extend(b.blocks.keys().cloned());
    keys.extend(o.blocks.keys().cloned());
    keys.extend(t.blocks.keys().cloned());

    let mut resolved: BTreeMap<String, Resolution> = BTreeMap::new();
    let mut present: BTreeMap<String, bool> = BTreeMap::new();
    for k in &keys {
        let r = resolve_key(k, &skip, b, o, t);
        present.insert(k.clone(), r.present);
        resolved.insert(k.clone(), r);
    }

    let order = reassemble(&o.order, &t.order, &present);
    let mut lines = Vec::new();
    let mut conflict = false;
    for k in &order {
        if let Some(r) = resolved.get(k) {
            for blk in &r.blocks {
                lines.extend(blk.iter().cloned());
            }
            conflict |= r.conflict;
        }
    }
    SectionMerge { lines, conflict }
}

fn resolve_key(
    k: &str,
    skip: &BTreeSet<String>,
    b: &Section,
    o: &Section,
    t: &Section,
) -> Resolution {
    let in_b = b.has(k);
    let in_o = o.has(k);
    let in_t = t.has(k);

    if skip.contains(k) {
        // Inherited corruption: presence only, carried through unchanged.
        let blocks = if in_o {
            o.blocks[k].clone()
        } else if in_t {
            t.blocks[k].clone()
        } else {
            Vec::new()
        };
        return Resolution {
            present: in_o || in_t,
            conflict: false,
            blocks,
        };
    }

    if in_o && in_t {
        let empty: Vec<String> = Vec::new();
        let base_blk: &[String] = if in_b { b.first(k) } else { &empty };
        let (blk, conf) = block_merge(base_blk, o.first(k), t.first(k));
        return Resolution {
            present: true,
            conflict: conf,
            blocks: vec![blk],
        };
    }

    if in_o != in_t {
        let (keeper, keeper_is_ours) = if in_o { (o, true) } else { (t, false) };
        let kept = keeper.first(k);
        if !in_b {
            // Added on one side, absent from base: keep it.
            return Resolution {
                present: true,
                conflict: false,
                blocks: vec![kept.to_vec()],
            };
        }
        if kept == b.first(k) {
            // Deleted on the other side, keeper unchanged: apply the deletion.
            return Resolution {
                present: false,
                conflict: false,
                blocks: Vec::new(),
            };
        }
        // Edited on one side, deleted on the other: a conflict.
        return Resolution {
            present: true,
            conflict: true,
            blocks: vec![edit_delete_block(kept, keeper_is_ours)],
        };
    }

    // Base only: both sides deleted it.
    Resolution {
        present: false,
        conflict: false,
        blocks: Vec::new(),
    }
}

// Two-way marker block for an edit on one side against a delete on the other.
fn edit_delete_block(kept: &[String], keeper_is_ours: bool) -> Vec<String> {
    let mut v = vec!["<<<<<<< ours".to_string()];
    if keeper_is_ours {
        v.extend(kept.iter().cloned());
    }
    v.push("=======".to_string());
    if !keeper_is_ours {
        v.extend(kept.iter().cloned());
    }
    v.push(">>>>>>> theirs".to_string());
    v
}

// Records in ours keep ours order. Records only in theirs attach after the
// nearest preceding record common to both, per SPEC 4.5.
fn reassemble(
    ours_order: &[String],
    theirs_order: &[String],
    present: &BTreeMap<String, bool>,
) -> Vec<String> {
    let ours_set: BTreeSet<&String> = ours_order.iter().collect();
    let is_present = |k: &String| present.get(k).copied().unwrap_or(false);

    let mut start_attach: Vec<String> = Vec::new();
    let mut after: BTreeMap<String, Vec<String>> = BTreeMap::new();
    let mut anchor: Option<String> = None;
    for k in theirs_order {
        if ours_set.contains(k) {
            anchor = Some(k.clone());
        } else if is_present(k) {
            match &anchor {
                Some(a) => after.entry(a.clone()).or_default().push(k.clone()),
                None => start_attach.push(k.clone()),
            }
        }
    }

    let mut out = Vec::new();
    out.extend(start_attach);
    for k in ours_order {
        if is_present(k) {
            out.push(k.clone());
        }
        if let Some(list) = after.get(k) {
            out.extend(list.iter().cloned());
        }
    }
    out
}

// Merge one record block line by line via the P5 engine. A '\n' is reattached
// per line so lines keep their identity, then the render is split back.
fn block_merge(base: &[String], ours: &[String], theirs: &[String]) -> (Vec<String>, bool) {
    let bt = term(base);
    let ot = term(ours);
    let tt = term(theirs);
    let br: Vec<&str> = bt.iter().map(String::as_str).collect();
    let orr: Vec<&str> = ot.iter().map(String::as_str).collect();
    let trr: Vec<&str> = tt.iter().map(String::as_str).collect();
    let regions = diff3::diff3(&br, &orr, &trr);
    let (text, conflict) = diff3::render_merge(&regions, &diff3::Labels::default());
    (unterm(&text), conflict)
}

fn term(lines: &[String]) -> Vec<String> {
    lines.iter().map(|l| format!("{l}\n")).collect()
}

fn unterm(text: &str) -> Vec<String> {
    let mut v: Vec<String> = text.split('\n').map(str::to_string).collect();
    if v.last().is_some_and(String::is_empty) {
        v.pop();
    }
    v
}

#[cfg(test)]
mod tests {
    use super::*;

    fn entry(id: &str, loc: &str) -> String {
        format!("  - m_Id: {id}\n    m_Localized: {loc}\n    m_Metadata:\n      m_Items: []")
    }

    fn entry_rids(id: &str, loc: &str, rids: &[&str]) -> String {
        let mut s =
            format!("  - m_Id: {id}\n    m_Localized: {loc}\n    m_Metadata:\n      m_Items:");
        for r in rids {
            s.push_str(&format!("\n      - rid: {r}"));
        }
        s
    }

    fn tbl(entries: &[String]) -> String {
        let mut s = String::from("--- !u!114 &1\nMonoBehaviour:\n  m_TableData:\n");
        for e in entries {
            s.push_str(e);
            s.push('\n');
        }
        s.push_str("  references:\n    version: 2\n");
        s
    }

    fn refrec(rid: &str, payload: &str, ids: &[&str]) -> String {
        let mut s =
            format!("    - rid: {rid}\n      {payload}\n      data:\n        m_SharedEntries:");
        for i in ids {
            s.push_str(&format!("\n        - id: {i}"));
        }
        s
    }

    fn refs(records: &[String]) -> String {
        let mut s = String::from(
            "--- !u!114 &1\nMonoBehaviour:\n  references:\n    version: 2\n    RefIds:\n",
        );
        for r in records {
            s.push_str(r);
            s.push('\n');
        }
        s
    }

    fn joined(m: &SectionMerge) -> String {
        m.lines.join("\n")
    }

    #[test]
    fn table_noop_keeps_all_entries() {
        let doc = tbl(&[entry("100", "a"), entry("200", "b")]);
        let m = merge_table(&doc, &doc, &doc);
        assert!(!m.conflict);
        assert_eq!(
            joined(&m),
            [entry("100", "a"), entry("200", "b")].join("\n")
        );
    }

    #[test]
    fn table_ours_edit_is_kept() {
        let base = tbl(&[entry("100", "a"), entry("200", "b")]);
        let ours = tbl(&[entry("100", "EDITED"), entry("200", "b")]);
        let theirs = base.clone();
        let m = merge_table(&base, &ours, &theirs);
        assert!(!m.conflict);
        assert_eq!(
            joined(&m),
            [entry("100", "EDITED"), entry("200", "b")].join("\n")
        );
    }

    #[test]
    fn table_theirs_add_appends_after_neighbor() {
        let base = tbl(&[entry("100", "a"), entry("200", "b")]);
        let ours = base.clone();
        let theirs = tbl(&[entry("100", "a"), entry("150", "new"), entry("200", "b")]);
        let m = merge_table(&base, &ours, &theirs);
        assert!(!m.conflict);
        assert_eq!(
            joined(&m),
            [entry("100", "a"), entry("150", "new"), entry("200", "b")].join("\n")
        );
    }

    #[test]
    fn table_clean_delete_drops_entry() {
        let base = tbl(&[entry("100", "a"), entry("200", "b")]);
        let ours = tbl(&[entry("100", "a")]);
        let theirs = base.clone();
        let m = merge_table(&base, &ours, &theirs);
        assert!(!m.conflict);
        assert_eq!(joined(&m), entry("100", "a"));
    }

    #[test]
    fn table_edit_delete_conflicts() {
        let base = tbl(&[entry("100", "a"), entry("200", "b")]);
        // ours deletes 200, theirs edits 200
        let ours = tbl(&[entry("100", "a")]);
        let theirs = tbl(&[entry("100", "a"), entry("200", "CHANGED")]);
        let m = merge_table(&base, &ours, &theirs);
        assert!(m.conflict);
        let text = joined(&m);
        assert!(text.contains("<<<<<<< ours"));
        assert!(text.contains(">>>>>>> theirs"));
        assert!(text.contains("CHANGED"));
    }

    #[test]
    fn table_both_edit_same_field_conflicts() {
        let base = tbl(&[entry("100", "a")]);
        let ours = tbl(&[entry("100", "OURS")]);
        let theirs = tbl(&[entry("100", "THEIRS")]);
        let m = merge_table(&base, &ours, &theirs);
        assert!(m.conflict);
        let text = joined(&m);
        assert!(text.contains("OURS"));
        assert!(text.contains("THEIRS"));
    }

    #[test]
    fn table_both_add_same_entry_is_clean() {
        let base = tbl(&[entry("100", "a")]);
        let add = entry("200", "b");
        let ours = tbl(&[entry("100", "a"), add.clone()]);
        let theirs = tbl(&[entry("100", "a"), add.clone()]);
        let m = merge_table(&base, &ours, &theirs);
        assert!(!m.conflict);
        assert_eq!(
            joined(&m),
            [entry("100", "a"), entry("200", "b")].join("\n")
        );
    }

    #[test]
    fn table_disjoint_edit_and_meta_add_merge_clean() {
        // ours edits the localized text, theirs adds a metadata rid: different
        // lines, so the block diff3 merges both without a conflict.
        let base = tbl(&[entry_rids("100", "a", &[])]);
        let ours = tbl(&[entry_rids("100", "EDIT", &[])]);
        let theirs = tbl(&[entry_rids("100", "a", &["5"])]);
        let m = merge_table(&base, &ours, &theirs);
        assert!(!m.conflict);
        let text = joined(&m);
        assert!(text.contains("m_Localized: EDIT"));
        assert!(text.contains("- rid: 5"));
    }

    #[test]
    fn refids_theirs_add_id_is_clean() {
        let base = refs(&[refrec("10", "type: A", &["1"])]);
        let ours = base.clone();
        let theirs = refs(&[refrec("10", "type: A", &["1", "2"])]);
        let m = merge_refids(&base, &ours, &theirs);
        assert!(!m.conflict);
        let text = joined(&m);
        assert!(text.contains("- id: 1"));
        assert!(text.contains("- id: 2"));
    }

    #[test]
    fn refids_new_record_appended() {
        let base = refs(&[refrec("10", "type: A", &["1"])]);
        let ours = base.clone();
        let theirs = refs(&[
            refrec("10", "type: A", &["1"]),
            refrec("20", "type: B", &["2"]),
        ]);
        let m = merge_refids(&base, &ours, &theirs);
        assert!(!m.conflict);
        let text = joined(&m);
        assert!(text.contains("- rid: 20"));
        assert!(text.contains("type: B"));
    }

    #[test]
    fn duplicate_key_is_carried_through_without_conflict() {
        // 100 duplicated in ours is inherited corruption: presence only, both
        // occurrences carried through, no content merge, no conflict.
        let base = tbl(&[entry("100", "a")]);
        let ours = tbl(&[entry("100", "a"), entry("100", "a")]);
        let theirs = base.clone();
        let m = merge_table(&base, &ours, &theirs);
        assert!(!m.conflict);
        assert_eq!(joined(&m).matches("m_Id: 100").count(), 2);
    }

    #[test]
    fn empty_inputs_yield_empty_merge() {
        let doc = "--- !u!1 &1\nGameObject:\n  m_Name: x\n";
        let m = merge_table(doc, doc, doc);
        assert!(!m.conflict);
        assert!(m.lines.is_empty());
    }
}
