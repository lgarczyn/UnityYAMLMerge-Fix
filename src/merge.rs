//! Keyed record merge, the structural core. SPEC section 4. Packet P6.
//! Builds the merged m_TableData and references/RefIds record sets from base,
//! ours and theirs by presence, content and reassembly rules.
//!
//! The reference is a verifier, validate_merge, not a constructor. This module
//! inverts its rules. Presence per SPEC 4.1, duplicates per 4.4 and record
//! order per 4.5 are explicit constructors here. A record present and changed
//! on both sides merges its id-list section by the set rule 4.3 as an explicit
//! constructor, packet P6b, so concurrent appends reach the union rather than
//! conflicting; the rest of the block merges line by line through the P5 diff3
//! engine, which realizes the scalar rule 4.2 and emits markers otherwise. The
//! block merge reuses raw bytes so output stays editor faithful. The P8
//! verifier is the backstop for the structural rules.
//!
//! Everything works on lines split on '\n' with no terminators, the same space
//! as the model parsers, so P7 can splice a merged section back into a document
//! by lines. Blocks are fed to diff3 with a '\n' reattached per line and the
//! rendered text is split back, which keeps line identity and bytes intact.
//!
//! Packet P7 adds document-level composition on top of the keyed core. The
//! document set is merged by the same presence rules as records, SPEC 4.1.
//! Each surviving document is dispatched: one that carries m_TableData or
//! references/RefIds routes those record runs through merge_table/merge_refids
//! as atomic placeholders while its other lines merge by diff3, SPEC 4.5;
//! everything else merges wholly by diff3, SPEC 4.6. The result is assembled
//! into one file in the same terminator-free line space.

use std::collections::{BTreeMap, BTreeSet};

use crate::diff3;
use crate::model::{self, Documents, Span};

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
        let (blk, conf) = merge_record_block(base_blk, o.first(k), t.first(k));
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

// Merge line by line via the P5 engine. A '\n' is reattached per line so lines
// keep their identity, then the render is split back. Used for record blocks,
// document bodies and preambles alike.
fn diff3_lines(base: &[String], ours: &[String], theirs: &[String]) -> (Vec<String>, bool) {
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

// --- P6b: set-rule constructor inside a both-changed record block ---------

// Placeholder standing in for a record's id-list run while the rest of the
// block merges by diff3. Same NUL convention as the P7 document placeholders,
// so it never collides with a real Unity YAML line.
const IDLIST_MARK: &str = "\u{0}uymerge-idlist\u{0}";

// The id-list region inside a record block: the `m_Items:` or
// `m_SharedEntries:` header line through its item run, with the header indent
// and name and each item value and original line. The empty flow form
// `m_Items: []` is a present empty list with no items. `start` indexes the
// header line, `end` is one past the region. `cr` records whether the header
// line ends in CR, so synthesized lines keep the record's terminator style
// and a CRLF or mixed file survives byte identical, SPEC 2.5.
struct IdList {
    start: usize,
    end: usize,
    indent: String,
    name: String,
    cr: bool,
    items: Vec<(String, String)>,
}

// Merge a record present and changed on both sides. The constructor owns the
// whole id-list region including its header so the SPEC 4.3 set rule alone
// decides the members; two branches appending different ids reach the union
// instead of conflicting, and a side that empties the list to `m_Items: []`
// cannot silently drop the other side's addition. The rest of the block merges
// by diff3 with the region masked to one placeholder line. A block with no
// id-list is a plain whole-block diff3, the P6 behavior.
fn merge_record_block(base: &[String], ours: &[String], theirs: &[String]) -> (Vec<String>, bool) {
    // SPEC 4.2: agreeing sides win verbatim. This also carries inherited
    // oddities like a value duplicated inside one list through a no-op
    // byte identical, instead of silently repairing them.
    if ours == theirs {
        return (ours.to_vec(), false);
    }
    let bl = find_idlist(base);
    let ol = find_idlist(ours);
    let tl = find_idlist(theirs);
    if bl.is_none() && ol.is_none() && tl.is_none() {
        return diff3_lines(base, ours, theirs);
    }

    // Region-level scalar rule first, SPEC 4.2 applied to the whole list
    // region as a byte unit: an unchanged side yields the other side's
    // region verbatim, which keeps its exact bytes including terminator
    // style. Only a list changed on both sides is constructed.
    let b_reg = region_lines(base, bl.as_ref());
    let o_reg = region_lines(ours, ol.as_ref());
    let t_reg = region_lines(theirs, tl.as_ref());
    let region = if o_reg == t_reg {
        o_reg
    } else if o_reg == b_reg {
        t_reg
    } else if t_reg == b_reg {
        o_reg
    } else {
        let ids = merge_id_set(bl.as_ref(), ol.as_ref(), tl.as_ref());
        // Header indent, name and terminator come from ours, the side whose
        // order also wins emission. The verifier compares normalized forms,
        // so the arbitrary choice on a genuine both-changed list is safe.
        match ol.as_ref().or(tl.as_ref()).or(bl.as_ref()) {
            Some(l) => emit_region(&l.indent, &l.name, l.cr, &ids),
            None => ids,
        }
    };
    let (masked, dconf) = diff3_lines(
        &mask_idlist(base, bl.as_ref()),
        &mask_idlist(ours, ol.as_ref()),
        &mask_idlist(theirs, tl.as_ref()),
    );

    let mut out = Vec::new();
    for line in masked {
        if line == IDLIST_MARK {
            out.extend(region.iter().cloned());
        } else {
            out.push(line);
        }
    }
    (out, dconf)
}

// A block's id-list region as raw lines, header through items; empty when
// the block has no list in either form.
fn region_lines(block: &[String], il: Option<&IdList>) -> Vec<String> {
    match il {
        Some(l) => block[l.start..l.end].to_vec(),
        None => Vec::new(),
    }
}

// The merged id-list region as lines: the canonical empty flow form when the
// set is empty, otherwise the bare header followed by the item lines. Indent
// and name are the block's own, so bytes stay editor faithful.
fn emit_region(indent: &str, name: &str, cr: bool, ids: &[String]) -> Vec<String> {
    let tail = if cr { "\r" } else { "" };
    if ids.is_empty() {
        vec![format!("{indent}{name}: []{tail}")]
    } else {
        let mut v = Vec::with_capacity(ids.len() + 1);
        v.push(format!("{indent}{name}:{tail}"));
        v.extend(ids.iter().cloned());
        v
    }
}

// Apply SPEC 4.3 to the three id lists: the merged set is
// (b & o & t) | (o - b) | (t - b), emitted in ours' order with theirs' new ids
// appended in theirs' order. Original item lines are reused so bytes stay
// editor faithful. The formula is total: an add/remove contradiction cannot
// exist with a shared base, so an id list never conflicts on its own.
fn merge_id_set(b: Option<&IdList>, o: Option<&IdList>, t: Option<&IdList>) -> Vec<String> {
    let bs = id_set(b);
    let os = id_set(o);
    let ts = id_set(t);

    let common = &(&bs & &os) & &ts;
    let o_add = &os - &bs;
    let t_add = &ts - &bs;
    let result = &(&common | &o_add) | &t_add;

    let mut emitted: BTreeSet<String> = BTreeSet::new();
    let mut lines = Vec::new();
    for src in [o, t].into_iter().flatten() {
        for (v, line) in &src.items {
            if result.contains(v) && emitted.insert(v.clone()) {
                lines.push(line.clone());
            }
        }
    }
    lines
}

fn id_set(il: Option<&IdList>) -> BTreeSet<String> {
    il.map(|l| l.items.iter().map(|(v, _)| v.clone()).collect())
        .unwrap_or_default()
}

// Replace a block's whole id-list region, header included, with a single
// placeholder line; everything else passes through. A block with no id-list
// passes through unchanged.
fn mask_idlist(lines: &[String], il: Option<&IdList>) -> Vec<String> {
    match il {
        None => lines.to_vec(),
        Some(il) => {
            let mut out = Vec::with_capacity(lines.len());
            out.extend_from_slice(&lines[..il.start]);
            out.push(IDLIST_MARK.to_string());
            out.extend_from_slice(&lines[il.end..]);
            out
        }
    }
}

// The first id-list region: an `m_Items:` or `m_SharedEntries:` header, either
// bare with a following run of `- rid:` or `- id:` items or the empty flow form
// `m_Items: []`. The empty form is a present list with no items, so a side that
// empties the list is still masked and owned by the set rule.
fn find_idlist(lines: &[String]) -> Option<IdList> {
    for (h, line) in lines.iter().enumerate() {
        let Some((indent, name, empty_form, cr)) = idlist_header(line) else {
            continue;
        };
        let mut end = h + 1;
        let mut items = Vec::new();
        if !empty_form {
            while end < lines.len() {
                match id_item_value(&lines[end]) {
                    Some(v) => {
                        items.push((v, lines[end].clone()));
                        end += 1;
                    }
                    None => break,
                }
            }
        }
        return Some(IdList {
            start: h,
            end,
            indent,
            name,
            cr,
            items,
        });
    }
    None
}

// An id-list header line as indent, field name, empty-flow-form flag and CR
// flag. Only `m_Items` and `m_SharedEntries` in bare or `[]` form qualify; an
// inline populated flow like `[1, 2]` is left to diff3. One trailing CR is
// tolerated and recorded, mirroring the CR-tolerant model matchers, so the
// region is owned in CRLF files too and synthesis keeps the terminator.
fn idlist_header(line: &str) -> Option<(String, String, bool, bool)> {
    let cr = line.ends_with('\r');
    let line = line.strip_suffix('\r').unwrap_or(line);
    let trimmed = line.trim_start_matches(char::is_whitespace);
    let indent_len = line.len() - trimmed.len();
    if indent_len == 0 {
        return None;
    }
    for name in ["m_Items", "m_SharedEntries"] {
        if let Some(rest) = trimmed.strip_prefix(name).and_then(|r| r.strip_prefix(':')) {
            return match rest {
                "" => Some((line[..indent_len].to_string(), name.to_string(), false, cr)),
                " []" => Some((line[..indent_len].to_string(), name.to_string(), true, cr)),
                _ => None,
            };
        }
    }
    None
}

// Value of an `- rid: N` or `- id: N` sequence item, else None. Matches the
// model item parsers: leading whitespace required, whole tail a signed int,
// one trailing CR tolerated and excluded from the value so ids compare equal
// across line-ending styles while emission reuses the original bytes.
fn id_item_value(line: &str) -> Option<String> {
    let line = line.strip_suffix('\r').unwrap_or(line);
    let trimmed = line.trim_start_matches(char::is_whitespace);
    if trimmed.len() == line.len() {
        return None;
    }
    let rest = trimmed
        .strip_prefix("- rid: ")
        .or_else(|| trimmed.strip_prefix("- id: "))?;
    let bytes = rest.as_bytes();
    let mut i = usize::from(bytes.first() == Some(&b'-'));
    let digits_start = i;
    while i < bytes.len() && bytes[i].is_ascii_digit() {
        i += 1;
    }
    if i == digits_start || i != bytes.len() {
        return None;
    }
    Some(rest.to_string())
}

// --- P7: document-level composition --------------------------------------

/// A merged whole file: the output lines in a terminator-free line space and
/// whether any conflict marker was emitted anywhere. Join with '\n' to render.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct FileMerge {
    pub lines: Vec<String>,
    pub conflict: bool,
}

// Placeholders standing in for a keyed record run while its document body
// merges by diff3. NUL cannot occur in Unity YAML text, so a placeholder never
// collides with a real line and stays a single stable line through the merge.
const TABLE_MARK: &str = "\u{0}uymerge-table\u{0}";
const REFIDS_MARK: &str = "\u{0}uymerge-refids\u{0}";

/// Merge three whole unwrapped files by document set and per-document dispatch.
/// Base, ours and theirs are the unwrapped texts; the result is the composed
/// file plus a conflict flag. No rewrap, no CRLF restore: that is the CLI's.
pub fn merge_file(base: &str, ours: &str, theirs: &str) -> FileMerge {
    let bl: Vec<&str> = base.split('\n').collect();
    let ol: Vec<&str> = ours.split('\n').collect();
    let tl: Vec<&str> = theirs.split('\n').collect();
    let bd = model::documents(base);
    let od = model::documents(ours);
    let td = model::documents(theirs);

    let mut out: Vec<String> = Vec::new();
    let mut conflict = false;

    // The preamble is a synthetic document; it merges as plain body content.
    let (pre, pconf) = diff3_lines(
        &span_lines(&bl, bd.preamble),
        &span_lines(&ol, od.preamble),
        &span_lines(&tl, td.preamble),
    );
    out.extend(pre);
    conflict |= pconf;

    // Anchors duplicated in any input are inherited corruption: presence only.
    let dups: BTreeSet<String> = bd
        .dups
        .iter()
        .chain(od.dups.iter())
        .chain(td.dups.iter())
        .cloned()
        .collect();

    let mut anchors: BTreeSet<String> = BTreeSet::new();
    anchors.extend(bd.docs.keys().cloned());
    anchors.extend(od.docs.keys().cloned());
    anchors.extend(td.docs.keys().cloned());

    let mut present: BTreeMap<String, bool> = BTreeMap::new();
    let mut content: BTreeMap<String, Vec<String>> = BTreeMap::new();
    for a in &anchors {
        let r = resolve_document(a, &dups, &bl, &bd, &ol, &od, &tl, &td);
        present.insert(a.clone(), r.present);
        conflict |= r.conflict;
        if r.present {
            content.insert(a.clone(), r.lines);
        }
    }

    // Documents in ours keep ours' order; theirs-only documents follow their
    // neighbor order, the same reassembly used for records, SPEC 4.5.
    let order = reassemble(&dedup(&od.order), &dedup(&td.order), &present);
    for a in &order {
        if let Some(lines) = content.get(a) {
            out.extend(lines.iter().cloned());
        }
    }
    FileMerge {
        lines: out,
        conflict,
    }
}

// Per-document merge outcome: whether it survives, whether it conflicted, and
// its output body lines.
struct DocResolution {
    present: bool,
    conflict: bool,
    lines: Vec<String>,
}

#[allow(clippy::too_many_arguments)]
fn resolve_document(
    a: &str,
    dups: &BTreeSet<String>,
    bl: &[&str],
    bd: &Documents,
    ol: &[&str],
    od: &Documents,
    tl: &[&str],
    td: &Documents,
) -> DocResolution {
    let in_b = bd.docs.contains_key(a);
    let in_o = od.docs.contains_key(a);
    let in_t = td.docs.contains_key(a);

    if dups.contains(a) {
        // Inherited corruption: presence only, no content merge. A duplicated
        // anchor is a corrupt input; the model keeps one span per anchor, so
        // this collapses it to the first occurrence, which the set-based
        // document verifier accepts. Record-level duplicates, the ones the
        // corpus actually shows, are carried through fully by the keyed merge.
        let lines = if in_o {
            doc_lines(ol, od, a)
        } else if in_t {
            doc_lines(tl, td, a)
        } else {
            Vec::new()
        };
        return DocResolution {
            present: in_o || in_t,
            conflict: false,
            lines,
        };
    }

    if in_o && in_t {
        let base_text = if in_b {
            doc_text(bl, bd, a)
        } else {
            String::new()
        };
        let (lines, conflict) =
            merge_document(&base_text, &doc_text(ol, od, a), &doc_text(tl, td, a));
        return DocResolution {
            present: true,
            conflict,
            lines,
        };
    }

    if in_o != in_t {
        let (kl, kd, keeper_is_ours) = if in_o {
            (ol, od, true)
        } else {
            (tl, td, false)
        };
        let kept = doc_lines(kl, kd, a);
        if !in_b {
            // Added on one side, absent from base: keep it.
            return DocResolution {
                present: true,
                conflict: false,
                lines: kept,
            };
        }
        if kept == doc_lines(bl, bd, a) {
            // Deleted on the other side, keeper unchanged: apply the deletion.
            return DocResolution {
                present: false,
                conflict: false,
                lines: Vec::new(),
            };
        }
        // Edited on one side, deleted on the other: a whole-document conflict.
        return DocResolution {
            present: true,
            conflict: true,
            lines: edit_delete_block(&kept, keeper_is_ours),
        };
    }

    // Base only: both sides deleted it.
    DocResolution {
        present: false,
        conflict: false,
        lines: Vec::new(),
    }
}

// Merge one document present on both sides. A document carrying keyed record
// runs merges those runs structurally and its remaining body by diff3 with the
// runs masked as atomic placeholders. A document with no keyed run merges
// wholly by diff3.
fn merge_document(base: &str, ours: &str, theirs: &str) -> (Vec<String>, bool) {
    let has_table = has_entries(base) || has_entries(ours) || has_entries(theirs);
    let has_refs = has_records(base) || has_records(ours) || has_records(theirs);
    if !has_table && !has_refs {
        return diff3_lines(&text_lines(base), &text_lines(ours), &text_lines(theirs));
    }
    let table = has_table.then(|| merge_table(base, ours, theirs));
    let refids = has_refs.then(|| merge_refids(base, ours, theirs));
    let (masked, dconf) = diff3_lines(&mask(base), &mask(ours), &mask(theirs));

    // A section conflict counts even when a surrounding delete drops its
    // placeholder, so a keyed conflict never slips out as a clean exit. The P8
    // verifier is the backstop that rechecks the assembled output.
    let mut conflict = dconf;
    if let Some(s) = &table {
        conflict |= s.conflict;
    }
    if let Some(s) = &refids {
        conflict |= s.conflict;
    }

    let mut out = Vec::new();
    for line in masked {
        if line == TABLE_MARK {
            if let Some(s) = &table {
                out.extend(s.lines.iter().cloned());
            }
        } else if line == REFIDS_MARK {
            if let Some(s) = &refids {
                out.extend(s.lines.iter().cloned());
            }
        } else {
            out.push(line);
        }
    }
    (out, conflict)
}

// Replace the table entry run and the RefIds record run with one placeholder
// line each; every other line passes through. The runs are contiguous by
// construction, so a placeholder holds the section's exact position.
fn mask(text: &str) -> Vec<String> {
    let lines: Vec<&str> = if text.is_empty() {
        Vec::new()
    } else {
        text.split('\n').collect()
    };
    let trun = table_run(text);
    let rrun = refids_run(text);
    let mut out = Vec::new();
    let mut i = 0;
    while i < lines.len() {
        if let Some((s, e)) = trun {
            if i == s {
                out.push(TABLE_MARK.to_string());
                i = e;
                continue;
            }
        }
        if let Some((s, e)) = rrun {
            if i == s {
                out.push(REFIDS_MARK.to_string());
                i = e;
                continue;
            }
        }
        out.push(lines[i].to_string());
        i += 1;
    }
    out
}

fn table_run(text: &str) -> Option<(usize, usize)> {
    span_bounds(
        model::table_entries(text)
            .entries
            .values()
            .flat_map(|e| e.spans.iter().copied()),
    )
}

fn refids_run(text: &str) -> Option<(usize, usize)> {
    span_bounds(
        model::refid_records(text)
            .records
            .values()
            .flat_map(|r| r.spans.iter().copied()),
    )
}

// Smallest start and largest end over a set of spans, or None when empty.
fn span_bounds(spans: impl Iterator<Item = Span>) -> Option<(usize, usize)> {
    let mut it = spans;
    let first = it.next()?;
    let mut lo = first.start;
    let mut hi = first.end;
    for s in it {
        lo = lo.min(s.start);
        hi = hi.max(s.end);
    }
    Some((lo, hi))
}

fn has_entries(text: &str) -> bool {
    !model::table_entries(text).order.is_empty()
}

fn has_records(text: &str) -> bool {
    !model::refid_records(text).order.is_empty()
}

fn span_lines(lines: &[&str], span: Option<Span>) -> Vec<String> {
    match span {
        Some(s) => lines[s.start..s.end]
            .iter()
            .map(|l| (*l).to_string())
            .collect(),
        None => Vec::new(),
    }
}

fn doc_text(lines: &[&str], docs: &Documents, a: &str) -> String {
    docs.docs
        .get(a)
        .map_or(String::new(), |s| lines[s.start..s.end].join("\n"))
}

fn doc_lines(lines: &[&str], docs: &Documents, a: &str) -> Vec<String> {
    span_lines(lines, docs.docs.get(a).copied())
}

// Split a body text into terminator-free lines, treating the empty text as no
// lines rather than one blank line, so an absent document contributes nothing.
fn text_lines(text: &str) -> Vec<String> {
    if text.is_empty() {
        Vec::new()
    } else {
        text.split('\n').map(str::to_string).collect()
    }
}

// First occurrence of each key, order preserved. Duplicate anchors would
// otherwise emit a document twice from one first-occurrence span.
fn dedup(order: &[String]) -> Vec<String> {
    let mut seen = BTreeSet::new();
    let mut out = Vec::new();
    for k in order {
        if seen.insert(k.clone()) {
            out.push(k.clone());
        }
    }
    out
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

    // --- P6b: set-rule constructor inside a both-changed record ----------

    #[test]
    fn terminator_flip_takes_theirs_region_bytes() {
        // Exotic battery E4a: theirs rewrites the file to CRLF, ours is
        // untouched. The id-list region must come from theirs verbatim, not
        // be resynthesized with ours' terminator, so take-theirs stays byte
        // exact end to end.
        let base = tbl(&[entry("100", "a"), entry("200", "b")]);
        let theirs = base.replace('\n', "\r\n");
        let m = merge_table(&base, &theirs, &base);
        assert!(!m.conflict);
        // merge_table emits the record section only; the byte-source truth
        // for take-theirs is theirs' own section, via a no-op on theirs.
        let want = merge_table(&theirs, &theirs, &theirs);
        assert_eq!(joined(&m), joined(&want));
        assert!(joined(&m).contains("m_Items: []\r"));
    }

    #[test]
    fn crlf_concurrent_add_keeps_terminators_and_unions() {
        // CRLF variant of the concurrent-add case. The region is owned under
        // CRLF too: ids compare equal across line endings, original item
        // bytes are reused and the synthesized header keeps the CR.
        let base = tbl(&[entry_rids("100", "a", &["1"])]).replace('\n', "\r\n");
        let ours = tbl(&[entry_rids("100", "a", &["1", "5"])]).replace('\n', "\r\n");
        let theirs = tbl(&[entry_rids("100", "a", &["1", "7"])]).replace('\n', "\r\n");
        let m = merge_table(&base, &ours, &theirs);
        assert!(!m.conflict);
        let text = joined(&m);
        assert!(text.contains("m_Items:\r"));
        assert!(text.contains("- rid: 1\r"));
        assert!(text.contains("- rid: 5\r"));
        assert!(text.contains("- rid: 7\r"));
    }

    #[test]
    fn table_concurrent_add_rids_unions() {
        // Both sides append a different rid to the same entry's m_Items list.
        // Plain diff3 conflicts on the shared insertion point; the set rule
        // merges to the union in ours-then-theirs order.
        let base = tbl(&[entry_rids("100", "a", &["1"])]);
        let ours = tbl(&[entry_rids("100", "a", &["1", "5"])]);
        let theirs = tbl(&[entry_rids("100", "a", &["1", "7"])]);
        let m = merge_table(&base, &ours, &theirs);
        assert!(!m.conflict);
        let text = joined(&m);
        assert!(text.contains("- rid: 1"));
        assert!(text.contains("- rid: 5"));
        assert!(text.contains("- rid: 7"));
    }

    #[test]
    fn table_concurrent_make_smart_from_empty_unions() {
        // The review scenario: two designers make the same string smart at
        // once, each adding a rid where base had `m_Items: []`. Both changes
        // land as the union.
        let base = tbl(&[entry("100", "a")]);
        let ours = tbl(&[entry_rids("100", "a", &["5"])]);
        let theirs = tbl(&[entry_rids("100", "a", &["7"])]);
        let m = merge_table(&base, &ours, &theirs);
        assert!(!m.conflict);
        let text = joined(&m);
        assert!(text.contains("- rid: 5"));
        assert!(text.contains("- rid: 7"));
    }

    #[test]
    fn table_add_and_remove_rid_merge_clean() {
        // Ours removes a rid, theirs adds a different one: both apply, no
        // conflict, per the set rule union and removal semantics.
        let base = tbl(&[entry_rids("100", "a", &["5", "7"])]);
        let ours = tbl(&[entry_rids("100", "a", &["7"])]);
        let theirs = tbl(&[entry_rids("100", "a", &["5", "7", "9"])]);
        let m = merge_table(&base, &ours, &theirs);
        assert!(!m.conflict);
        let text = joined(&m);
        assert!(!text.contains("- rid: 5"));
        assert!(text.contains("- rid: 7"));
        assert!(text.contains("- rid: 9"));
    }

    #[test]
    fn table_set_merge_keeps_text_conflict() {
        // The id lists union cleanly, but the localized text is edited
        // differently on both sides, so the block still conflicts by diff3.
        let base = tbl(&[entry_rids("100", "a", &["1"])]);
        let ours = tbl(&[entry_rids("100", "OURS", &["1", "5"])]);
        let theirs = tbl(&[entry_rids("100", "THEIRS", &["1", "7"])]);
        let m = merge_table(&base, &ours, &theirs);
        assert!(m.conflict);
        let text = joined(&m);
        assert!(text.contains("OURS"));
        assert!(text.contains("THEIRS"));
    }

    #[test]
    fn table_ours_empties_while_theirs_adds_keeps_addition() {
        // Ours removes every rid, so its block carries `m_Items: []`; theirs
        // keeps rid 1 and adds rid 7. The set rule owns the whole region, so
        // theirs' addition is not dropped by a masked-header diff3.
        let base = tbl(&[entry_rids("100", "a", &["1"])]);
        let ours = tbl(&[entry("100", "a")]);
        let theirs = tbl(&[entry_rids("100", "a", &["1", "7"])]);
        let m = merge_table(&base, &ours, &theirs);
        assert!(!m.conflict);
        let text = joined(&m);
        assert!(!text.contains("- rid: 1"));
        assert!(text.contains("- rid: 7"));
        assert!(!text.contains("m_Items: []"));
    }

    #[test]
    fn table_theirs_empties_while_ours_adds_keeps_addition() {
        let base = tbl(&[entry_rids("100", "a", &["1"])]);
        let ours = tbl(&[entry_rids("100", "a", &["1", "5"])]);
        let theirs = tbl(&[entry("100", "a")]);
        let m = merge_table(&base, &ours, &theirs);
        assert!(!m.conflict);
        let text = joined(&m);
        assert!(!text.contains("- rid: 1"));
        assert!(text.contains("- rid: 5"));
        assert!(!text.contains("m_Items: []"));
    }

    #[test]
    fn table_both_empty_the_list_yields_empty_form() {
        // Both sides remove every rid; the merged region is the canonical
        // empty flow form, not a bare header.
        let base = tbl(&[entry_rids("100", "a", &["1"])]);
        let ours = tbl(&[entry("100", "a")]);
        let theirs = tbl(&[entry("100", "a")]);
        let m = merge_table(&base, &ours, &theirs);
        assert!(!m.conflict);
        let text = joined(&m);
        assert!(!text.contains("- rid: 1"));
        assert!(text.contains("m_Items: []"));
    }

    #[test]
    fn refids_ours_empties_while_theirs_adds_keeps_addition() {
        let base = refs(&[refrec("10", "type: A", &["1"])]);
        let ours = refs(&[refrec("10", "type: A", &[])]);
        let theirs = refs(&[refrec("10", "type: A", &["1", "2"])]);
        let m = merge_refids(&base, &ours, &theirs);
        assert!(!m.conflict);
        let text = joined(&m);
        assert!(!text.contains("- id: 1"));
        assert!(text.contains("- id: 2"));
        assert!(!text.contains("m_SharedEntries: []"));
    }

    #[test]
    fn refids_theirs_empties_while_ours_adds_keeps_addition() {
        let base = refs(&[refrec("10", "type: A", &["1"])]);
        let ours = refs(&[refrec("10", "type: A", &["1", "3"])]);
        let theirs = refs(&[refrec("10", "type: A", &[])]);
        let m = merge_refids(&base, &ours, &theirs);
        assert!(!m.conflict);
        let text = joined(&m);
        assert!(!text.contains("- id: 1"));
        assert!(text.contains("- id: 3"));
        assert!(!text.contains("m_SharedEntries: []"));
    }

    #[test]
    fn refids_concurrent_add_ids_unions() {
        // Both sides append a different id to the same record's
        // m_SharedEntries list. The set rule merges to the union.
        let base = refs(&[refrec("10", "type: A", &["1"])]);
        let ours = refs(&[refrec("10", "type: A", &["1", "2"])]);
        let theirs = refs(&[refrec("10", "type: A", &["1", "3"])]);
        let m = merge_refids(&base, &ours, &theirs);
        assert!(!m.conflict);
        let text = joined(&m);
        assert!(text.contains("- id: 1"));
        assert!(text.contains("- id: 2"));
        assert!(text.contains("- id: 3"));
    }

    // --- P7: document composition ----------------------------------------

    const PREFAB: &str = include_str!("../tests/fixtures/inputs/prefab-multidoc.prefab");
    const TABLE: &str = include_str!("../tests/fixtures/inputs/table-with-refs.asset");

    fn rendered(m: &FileMerge) -> String {
        m.lines.join("\n")
    }

    #[test]
    fn file_noop_multidoc_is_byte_identical() {
        let m = merge_file(PREFAB, PREFAB, PREFAB);
        assert!(!m.conflict);
        assert_eq!(rendered(&m), PREFAB);
    }

    #[test]
    fn record_payload_closing_at_column_zero_survives_noop() {
        // P10 regression: a quoted payload can close at column zero. The
        // content scan stops there but the emission span must not, or the
        // mask swallows the close quote and re-emission un-terminates the
        // scalar, mangling every later record.
        let doc = "%YAML 1.1\n%TAG !u! tag:unity3d.com,2011:\n\
                   --- !u!114 &11400000\nMonoBehaviour:\n  m_Name: Shared\n\
                   \x20 references:\n    version: 2\n    RefIds:\n\
                   \x20   - rid: 100\n      data:\n        m_CommentText: '\n\n'\n\
                   \x20   - rid: 200\n      data:\n        m_CommentText: after\n";
        let m = merge_file(doc, doc, doc);
        assert!(!m.conflict);
        assert_eq!(rendered(&m), doc);
    }

    #[test]
    fn within_list_duplicate_rid_survives_noop() {
        // P10 regression: an entry carrying the same rid twice is inherited
        // corruption; agreeing sides pass through verbatim rather than being
        // silently deduplicated on a no-op.
        let base = tbl(&[entry_rids("100", "a", &["7", "7"])]);
        let m = merge_table(&base, &base, &base);
        assert!(!m.conflict);
        assert_eq!(joined(&m).matches("- rid: 7").count(), 2);
    }

    #[test]
    fn file_noop_keyed_doc_is_byte_identical() {
        // A document with both m_TableData and references/RefIds round-trips
        // through masking, keyed merge and reassembly with no byte change.
        let m = merge_file(TABLE, TABLE, TABLE);
        assert!(!m.conflict);
        assert_eq!(rendered(&m), TABLE);
    }

    #[test]
    fn document_added_on_theirs_appends_by_neighbor() {
        let base = "%YAML 1.1\n--- !u!1 &100\nGameObject:\n  m_Name: A\n";
        let ours = base;
        let theirs = "%YAML 1.1\n--- !u!1 &100\nGameObject:\n  m_Name: A\n--- !u!1 &200\nGameObject:\n  m_Name: B\n";
        let m = merge_file(base, ours, theirs);
        assert!(!m.conflict);
        assert_eq!(rendered(&m), theirs);
    }

    #[test]
    fn document_deleted_on_ours_is_dropped() {
        let base = "%YAML 1.1\n--- !u!1 &100\nGameObject:\n  m_Name: A\n--- !u!1 &200\nGameObject:\n  m_Name: B\n";
        let ours = "%YAML 1.1\n--- !u!1 &100\nGameObject:\n  m_Name: A\n";
        let theirs = base;
        let m = merge_file(base, ours, theirs);
        assert!(!m.conflict);
        assert_eq!(rendered(&m), ours);
    }

    #[test]
    fn document_added_both_sides_identically_is_clean() {
        let base = "%YAML 1.1\n--- !u!1 &100\nGameObject:\n  m_Name: A\n";
        let add = "%YAML 1.1\n--- !u!1 &100\nGameObject:\n  m_Name: A\n--- !u!1 &200\nGameObject:\n  m_Name: B\n";
        let m = merge_file(base, add, add);
        assert!(!m.conflict);
        assert_eq!(rendered(&m), add);
    }

    #[test]
    fn document_edit_delete_conflicts() {
        let base = "%YAML 1.1\n--- !u!1 &100\nGameObject:\n  m_Name: A\n--- !u!1 &200\nGameObject:\n  m_Name: B\n";
        // ours deletes 200, theirs edits it
        let ours = "%YAML 1.1\n--- !u!1 &100\nGameObject:\n  m_Name: A\n";
        let theirs = "%YAML 1.1\n--- !u!1 &100\nGameObject:\n  m_Name: A\n--- !u!1 &200\nGameObject:\n  m_Name: CHANGED\n";
        let m = merge_file(base, ours, theirs);
        assert!(m.conflict);
        let text = rendered(&m);
        assert!(text.contains("<<<<<<< ours"));
        assert!(text.contains(">>>>>>> theirs"));
        assert!(text.contains("CHANGED"));
    }

    #[test]
    fn plain_document_body_conflicts_by_diff3() {
        let base = "%YAML 1.1\n--- !u!114 &1\nMonoBehaviour:\n  m_Value: 1\n";
        let ours = "%YAML 1.1\n--- !u!114 &1\nMonoBehaviour:\n  m_Value: 2\n";
        let theirs = "%YAML 1.1\n--- !u!114 &1\nMonoBehaviour:\n  m_Value: 3\n";
        let m = merge_file(base, ours, theirs);
        assert!(m.conflict);
        let text = rendered(&m);
        assert!(text.contains("  m_Value: 2"));
        assert!(text.contains("  m_Value: 3"));
    }

    #[test]
    fn plain_document_disjoint_edits_merge_clean() {
        // The two edits sit on either side of an unchanged line, so diff3 keeps
        // them apart and merges both, matching git merge-file.
        let base = "%YAML 1.1\n--- !u!114 &1\nMonoBehaviour:\n  a: 1\n  m: 0\n  b: 1\n";
        let ours = "%YAML 1.1\n--- !u!114 &1\nMonoBehaviour:\n  a: 9\n  m: 0\n  b: 1\n";
        let theirs = "%YAML 1.1\n--- !u!114 &1\nMonoBehaviour:\n  a: 1\n  m: 0\n  b: 9\n";
        let m = merge_file(base, ours, theirs);
        assert!(!m.conflict);
        assert_eq!(
            rendered(&m),
            "%YAML 1.1\n--- !u!114 &1\nMonoBehaviour:\n  a: 9\n  m: 0\n  b: 9\n"
        );
    }

    // A document body edit and a keyed entry edit on opposite sides must both
    // land: the body merges by diff3, the entries by the keyed rules, and the
    // record run stays out of the body diff3 as an atomic placeholder.
    #[test]
    fn keyed_and_body_edits_on_opposite_sides_merge() {
        let base = "%YAML 1.1\n--- !u!114 &1\nMonoBehaviour:\n  m_Name: T\n  m_TableData:\n  - m_Id: 100\n    m_Localized: a\n  references:\n    version: 2\n";
        // ours edits the entry text, theirs edits the body name
        let ours = "%YAML 1.1\n--- !u!114 &1\nMonoBehaviour:\n  m_Name: T\n  m_TableData:\n  - m_Id: 100\n    m_Localized: EDIT\n  references:\n    version: 2\n";
        let theirs = "%YAML 1.1\n--- !u!114 &1\nMonoBehaviour:\n  m_Name: RENAMED\n  m_TableData:\n  - m_Id: 100\n    m_Localized: a\n  references:\n    version: 2\n";
        let m = merge_file(base, ours, theirs);
        assert!(!m.conflict);
        let text = rendered(&m);
        assert!(text.contains("m_Name: RENAMED"));
        assert!(text.contains("m_Localized: EDIT"));
    }

    // Adding an entry on one side and a whole record on the other, in the same
    // keyed document, must merge both without conflict.
    #[test]
    fn keyed_entry_and_record_adds_merge() {
        let base = "%YAML 1.1\n--- !u!114 &1\nMonoBehaviour:\n  m_TableData:\n  - m_Id: 100\n    m_Localized: a\n  references:\n    version: 2\n    RefIds:\n    - rid: 10\n      type: A\n";
        let ours = "%YAML 1.1\n--- !u!114 &1\nMonoBehaviour:\n  m_TableData:\n  - m_Id: 100\n    m_Localized: a\n  - m_Id: 200\n    m_Localized: b\n  references:\n    version: 2\n    RefIds:\n    - rid: 10\n      type: A\n";
        let theirs = "%YAML 1.1\n--- !u!114 &1\nMonoBehaviour:\n  m_TableData:\n  - m_Id: 100\n    m_Localized: a\n  references:\n    version: 2\n    RefIds:\n    - rid: 10\n      type: A\n    - rid: 20\n      type: B\n";
        let m = merge_file(base, ours, theirs);
        assert!(!m.conflict);
        let text = rendered(&m);
        assert!(text.contains("m_Id: 200"));
        assert!(text.contains("- rid: 20"));
    }

    #[test]
    fn preamble_edit_on_one_side_is_kept() {
        let base = "%YAML 1.1\n%TAG !u! x\n--- !u!1 &1\nGameObject:\n  m_Name: A\n";
        let ours = "%YAML 1.1\n%TAG !u! y\n--- !u!1 &1\nGameObject:\n  m_Name: A\n";
        let theirs = base;
        let m = merge_file(base, ours, theirs);
        assert!(!m.conflict);
        assert_eq!(rendered(&m), ours);
    }

    #[test]
    fn duplicate_anchor_collapses_without_conflict() {
        // A duplicated anchor is corrupt input. Presence rules apply with no
        // content merge; it collapses to the first occurrence and never
        // conflicts forever.
        let doc = "%YAML 1.1\n--- !u!1 &100\nGameObject:\n  m_Name: A\n--- !u!1 &100\nGameObject:\n  m_Name: B\n";
        let m = merge_file(doc, doc, doc);
        assert!(!m.conflict);
        let text = rendered(&m);
        assert_eq!(text.matches("&100").count(), 1);
        assert!(text.contains("m_Name: A"));
    }
}
