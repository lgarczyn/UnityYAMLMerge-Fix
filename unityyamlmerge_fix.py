#!/usr/bin/env python3
"""UnityYAMLMerge-Fix: a git merge driver that makes Unity's Smart Merge produce
editor-identical output.

UnityYAMLMerge merges Unity scenes/prefabs/assets well but its output diverges from what the
editor writes, so merged files churn and sometimes lose data. The differences, and what this fixes:

  - significant whitespace before a fold is dropped (silent data loss)
  - scalars are wrapped at the wrong width
  - CRLF is normalized to LF
  - empty flow values become `''`, flow mappings wrap, bare-sequence scalars don't fold

It runs the native UnityYAMLMerge but unwraps the inputs first (so the standard-YAML parser can't
strip fold-trailing whitespace) and re-wraps the output exactly as the editor would, restoring the
original line endings. Pure Python, standard library only, one file, every platform and Unity
version. If anything goes wrong it falls back to a plain native merge, so it can never break a merge.

Usage as a git merge driver (see README): unityyamlmerge_fix.py BASE REMOTE LOCAL OUTPUT
"""
import glob
import os
import re
import shutil
import subprocess
import sys
import tempfile

# A `key: value` mapping line; the leading group absorbs sequence dashes (`- k: v`, nested `- - k: v`)
# so the dash counts toward the indent and the value still folds. Group 3 keeps trailing spaces.
KEY = re.compile(r"^(\s*(?:- )*)([\w.\-/]+):\s(.+)$")
# A bare plain scalar as a sequence item (`- Assembly.Type, Name, Version=...`), no `key:` form.
SEQ = re.compile(r"^(\s*)- (\S.*)$")
MAPPINGISH = re.compile(r"^[\w.\-/]+:(\s|$)")   # `key:` / `key: ...`, a mapping item not a scalar
EXCLUDE_FIRST = set("|>{[&*!#%")                # value heads that are not plain scalars
EMPTY_FLOW = re.compile(r": ''(?=[,}])")        # the merge's `{a: ''}` injection; editor leaves bare

PLAIN_WIDTH = 79
QUOTED_WIDTH = 80
INF = 10 ** 9                                   # "unwrap" width: never fold


# --- editor-faithful re-wrap codec -------------------------------------------------------------
# Works on raw text, never decoding to a value: Unity's pre-fold whitespace is significant, so the
# oracle is byte-equality. A plain scalar breaks at the last space of a run past the width, keeping
# earlier spaces as trailing whitespace; quoted scalars fold at best_width keeping significant space.

def reemit_plain(value, prefix, cont_indent, width):
    out, cur, col, n = [], prefix, len(prefix), len(value)
    for i, c in enumerate(value):
        if c == " " and col > width and not (i + 1 < n and value[i + 1] == " "):
            out.append(cur)
            cur, col = cont_indent, len(cont_indent)
        else:
            cur += c
            col += 1
    out.append(cur)
    return out


def join_plain_value(val, conts, cont_indent):
    # inverse of reemit_plain: each continuation contributes one fold space plus its content
    return val + "".join(" " + c[len(cont_indent):] for c in conts)


def gather_continuations(lines, i, key_indent):
    # strictly-more-indented non-blank lines that are not themselves mappings
    conts, j = [], i + 1
    while j < len(lines):
        c = lines[j]
        ci = len(c) - len(c.lstrip(" "))
        if c.strip() == "" or ci <= key_indent or KEY.match(c):
            break
        conts.append(c)
        j += 1
    return conts, j


def gather_quoted(lines, i, quote_col, quote):
    # span a quoted block to its matching close; delimited by the quote, not indent. `''` and
    # `\"`/`\\` are escapes, not closes, and the value may contain blank lines and `key:` prose.
    s = "\n".join(lines[i:])
    k, n = quote_col + 1, len("\n".join(lines[i:]))
    while k < n:
        c = s[k]
        if quote == "'":
            if c == "'":
                if k + 1 < n and s[k + 1] == "'":
                    k += 2
                    continue
                break
        else:
            if c == "\\":
                k += 2
                continue
            if c == '"':
                break
        k += 1
    j = min(i + s[:k + 1].count("\n") + 1, len(lines))
    return lines[i:j], j


def reemit_quoted(content, prefix, cont_indent, width):
    # single-quoted: ' -> ''; content newline = blank line; column accumulates across newlines
    # (first +cont+2, each extra +1); a fold never splits a space run; trailing \n closes alone.
    if content == "":
        return [prefix + "'"]
    segments = content.replace("'", "''").split("\n")
    out, vcol, ind, prev_empty = [], len(prefix), " " * cont_indent, False
    for s, seg in enumerate(segments):
        if s > 0:
            out.append("")
            vcol += 1 if prev_empty else cont_indent + 2
        if seg == "":
            if s == 0:
                out.append(prefix)
            prev_empty = s != 0
            continue
        prev_empty = False
        cur, firstw = (prefix if s == 0 else ind), True
        for word in seg.split(" "):
            if firstw:
                cur += word
                vcol += len(word)
                firstw = False
            elif word != "" and vcol + 1 > width:
                out.append(cur)
                cur, vcol = ind + word, cont_indent + len(word)
            else:
                cur += " " + word
                vcol += 1 + len(word)
        out.append(cur)
    if segments[-1] == "":
        out.append("'")
    else:
        out[-1] += "'"
    return out


def decode_quoted(block, prefix, cont_indent):
    # inverse of reemit_quoted: soft fold = one restored space, blank line = one newline, spaces kept
    if block[-1].strip() == "'":
        body = block[:-1]
    else:
        body = block[:-1] + [block[-1][:block[-1].rfind("'")]]
    phys = [body[0][len(prefix):]] + ["" if l.strip() == "" else l[cont_indent:] for l in body[1:]]
    content = phys[0]
    for k in range(1, len(phys)):
        if phys[k] == "":
            content += "\n"
        elif phys[k - 1] == "":
            content += phys[k]
        else:
            content += " " + phys[k]
    return content.replace("''", "'")


def reemit_double(escaped, prefix, cont_indent, width):
    # double-quoted is one continuous escaped flow (\n, \uXXXX, ... are spaceless), so just re-wrap
    out, cur, vcol, ind, first = [], prefix, len(prefix), " " * cont_indent, True
    for word in escaped.split(" "):
        if first:
            cur += word
            vcol += len(word)
            first = False
        elif word != "" and vcol + 1 > width:
            out.append(cur)
            cur, vcol = ind + word, cont_indent + len(word)
        else:
            cur += " " + word
            vcol += 1 + len(word)
    out.append(cur + '"')
    return out


def decode_double(block, prefix, cont_indent):
    # soft folds rejoined (one space each); `\ ` (escaped space) -> literal space, otherwise verbatim
    body = block[:-1] + [block[-1][:block[-1].rfind('"')]]
    s = " ".join([body[0][len(prefix):]] + [l[cont_indent:] for l in body[1:]])
    out, i, n = [], 0, len(s)
    while i < n:
        if s[i] == "\\" and i + 1 < n:
            out.append(" " if s[i + 1] == " " else s[i:i + 2])
            i += 2
        else:
            out.append(s[i])
            i += 1
    return "".join(out)


def split_lines(text):
    # (content, had_cr) per line, preserving each terminator (some assets carry mixed LF/CRLF)
    return [(p[:-1], True) if p.endswith("\r") else (p, False) for p in text.split("\n")]


def reserialize(text, width=PLAIN_WIDTH, quoted_width=QUOTED_WIDTH, fix_empty=True):
    """Re-wrap `text` the way the Unity editor would. Plain scalars fold at `width`, quoted at
    `quoted_width` (use INF to unwrap). Quoted blocks with mixed LF/CRLF pass through verbatim, since
    a terminator is never invented. Idempotent on editor-form input; fold lines inherit the block's
    first-line terminator. With fix_empty, the merge's injected `''` flow values are stripped."""
    pairs = split_lines(text)
    lines = [c for c, _ in pairs]
    crs = [cr for _, cr in pairs]
    out, i, n = [], 0, len(lines)
    while i < n:
        line = lines[i]
        m = KEY.match(line)
        if not m:
            sm = SEQ.match(line)
            if (sm and sm.group(2)[0] not in EXCLUDE_FIRST and sm.group(2)[0] not in "'\""
                    and not sm.group(2).startswith("- ") and not MAPPINGISH.match(sm.group(2))):
                conts, j = gather_continuations(lines, i, len(sm.group(1)))
                prefix = line[: len(line) - len(sm.group(2))]
                cont_indent = (" " * (len(conts[0]) - len(conts[0].lstrip(" ")))
                               if conts else " " * len(prefix))
                value = join_plain_value(sm.group(2), conts, cont_indent)
                out.extend((e, crs[i]) for e in reemit_plain(value, prefix, cont_indent, width))
                i = j
                continue
            out.append((line, crs[i]))
            i += 1
            continue
        indent, _key, val = m.groups()
        first = val[0]
        if first in EXCLUDE_FIRST:
            out.append((line, crs[i]))
            i += 1
            continue
        if first in ("'", '"'):
            block, j = gather_quoted(lines, i, len(line) - len(val), first)
            if len(set(crs[i:j])) == 1:
                qp = line[: len(line) - len(val) + 1]
                inner = [c for c in block[1:-1] if c.strip()]
                ci = (len(inner[0]) - len(inner[0].lstrip(" "))) if inner else len(indent) + 2
                decode = decode_quoted if first == "'" else decode_double
                reemit = reemit_quoted if first == "'" else reemit_double
                out.extend((e, crs[i]) for e in reemit(decode(block, qp, ci), qp, ci, quoted_width))
            else:
                out.extend((lines[k], crs[k]) for k in range(i, j))
            i = j
            continue
        conts, j = gather_continuations(lines, i, len(indent))
        prefix = line[: len(line) - len(val)]
        cont_indent = (" " * (len(conts[0]) - len(conts[0].lstrip(" ")))
                       if conts else " " * (len(indent) + 2))
        value = join_plain_value(val, conts, cont_indent)
        out.extend((e, crs[i]) for e in reemit_plain(value, prefix, cont_indent, width))
        i = j
    result = "\n".join(c + ("\r" if cr else "") for c, cr in out)
    return EMPTY_FLOW.sub(": ", result) if fix_empty else result


# --- structural verification ---------------------------------------------
# UnityYAMLMerge is line-based: dense edits near long multi-line scalars
# can drop or misalign records and still exit 0. Everything id-keyed is
# therefore verified against true 3-way semantics on the unwrapped forms.
# String-table entries are keyed by m_Id, text and metadata rid set.
# SerializeReference records are keyed by rid, payload and m_SharedEntries
# id set. Whole YAML documents are keyed by their &anchor. A clean exit is
# a verified merge. Any deviation is escalated: healed by a validated text
# merge when possible, otherwise the merge fails with conflict markers.
# Git keeps the driver's output on failure, and a clean-looking lossy
# file gets committed as-is.

ENTRY_ID = re.compile(r"^  - m_Id: (\d+)\s*$")
REF_RID = re.compile(r"^    - rid: (-?\d+)\s*$")
ITEM_RID = re.compile(r"^\s+- rid: (-?\d+)$")
SHARED_ID = re.compile(r"^\s+- id: (-?\d+)$")
DOC_ANCHOR = re.compile(r"^--- !u!\d+ &(-?\d+)", re.M)


def table_entries(text):
    """m_TableData records: id to localized text, metadata rid set and
    m_Localized count. Returns entries plus the duplicate ids."""
    entries, dups = {}, set()
    in_table, cur, field = False, None, None
    for line in text.split("\n"):
        if line.startswith("  ") and not line.startswith("  -") and line[2:3].strip():
            in_table = line.strip() == "m_TableData:"   # any other 2-indent key ends the table
            cur = None
            continue
        if not in_table:
            continue
        m = ENTRY_ID.match(line)
        if m:
            if m.group(1) in entries:
                dups.add(m.group(1))
            cur = entries.setdefault(m.group(1), {"loc": [], "n": 0, "rids": set()})
            field = None
            continue
        if cur is None:
            continue
        if line.startswith("    m_Localized:"):
            cur["loc"].append(line[16:])
            cur["n"] += 1
            field = "loc"
        elif line.startswith("    m_"):
            field = "meta" if line.startswith("    m_Metadata:") else None
        elif field == "meta" and ITEM_RID.match(line):
            cur["rids"].add(ITEM_RID.match(line).group(1))
        elif field == "loc":                             # continuation of an unwrapped value
            cur["loc"].append(line)
    return ({k: ("\n".join(v["loc"]), frozenset(v["rids"]), v["n"])
             for k, v in entries.items()}, dups)


def refid_records(text):
    """SerializeReference records under references/RefIds: rid to payload
    text without id items plus the `- id:` value set. Returns records plus
    the duplicate rids."""
    recs, dups = {}, set()
    in_refs, in_list, cur = False, False, None
    for line in text.split("\n"):
        if line.startswith("  ") and not line.startswith("  -") and line[2:3].strip():
            in_refs, in_list, cur = line.strip() == "references:", False, None
            continue
        if not in_refs:
            continue
        if line == "    RefIds:":
            in_list = True
            continue
        if not in_list:
            continue
        m = REF_RID.match(line)
        if m:
            if m.group(1) in recs:
                dups.add(m.group(1))
            cur = recs.setdefault(m.group(1), {"raw": [], "ids": set()})
            continue
        if cur is not None and (line.startswith("      ") or line == ""):
            m = SHARED_ID.match(line)
            if m:
                cur["ids"].add(m.group(1))
            else:
                cur["raw"].append(line)
        else:
            cur = None
    return ({k: ("\n".join(v["raw"]), frozenset(v["ids"])) for k, v in recs.items()}, dups)


def dedup_refids(text):
    """Drop byte-identical duplicate RefIds records, a known benign churn of
    the native merge. Duplicates that differ are left for verification to
    reject."""
    lines = text.split("\n")
    out, seen = [], {}
    in_refs, in_list = False, False
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        if line.startswith("  ") and not line.startswith("  -") and line[2:3].strip():
            in_refs, in_list = line.strip() == "references:", False
        elif in_refs and line == "    RefIds:":
            in_list = True
        m = REF_RID.match(line) if in_list else None
        if not m:
            out.append(line)
            i += 1
            continue
        j = i + 1
        while j < n and lines[j].startswith("      "):
            j += 1
        block = lines[i:j]
        if seen.get(m.group(1)) == block:
            i = j                                        # identical duplicate: drop it
            continue
        seen.setdefault(m.group(1), block)
        out.extend(block)
        i = j
    return "\n".join(out)


def _value_rule(what, b, o, t, m, viols):
    # scalar 3-way: a silent side-pick or an invented value is as much a
    # loss as a drop
    if o == t:
        if m != o:
            viols.append(what + " matches neither side")
    elif b == o:
        if m != t:
            viols.append(what + " lost theirs' change")
    elif b == t:
        if m != o:
            viols.append(what + " lost ours' change")
    else:
        viols.append(what + " changed differently on both sides")


def _set_rule(what, b, o, t, m, viols):
    # id sets merge as sets: both sides' additions and removals must all
    # be honored. An add/remove contradiction cannot exist with a shared
    # base, so the formula is total.
    b = b or frozenset()
    if m != (b & o & t) | (o - b) | (t - b):
        viols.append(what + " does not match the 3-way id set")


def _presence_rule(what, key, b, o, t, m, viols, verify_both, verify_copy):
    in_o, in_t, in_m = key in o, key in t, key in m
    if in_o and in_t:
        if not in_m:
            viols.append(what + " was dropped (present on both sides)")
        else:
            verify_both(key)
    elif in_o or in_t:
        side, other = (o, "theirs") if in_o else (t, "ours")
        if key not in b:
            if not in_m:
                viols.append(what + " added on one side was dropped")
            else:
                verify_copy(key, side)
        elif side[key] == b[key]:
            if in_m:
                viols.append(what + " deleted on " + other + " was resurrected")
        else:
            viols.append(what + " edited on one side but deleted on " + other)
    elif in_m and key in b:
        viols.append(what + " deleted on both sides was resurrected")


def validate_merge(base, ours, theirs, merged):
    """Every violation of faithful 3-way merge semantics. Empty is verified."""
    base, ours, theirs, merged = (x.replace("\r\n", "\n") for x in (base, ours, theirs, merged))
    viols = []
    ab, ao, at, am = (set(DOC_ANCHOR.findall(x)) for x in (base, ours, theirs, merged))
    for a in sorted((ao & at) - am):
        viols.append("document &" + a + " was dropped (present on both sides)")
    for a in sorted((ao | at) - (ao & at) - ab - am):
        viols.append("document &" + a + " added on one side was dropped")
    (eb, edb), (eo, edo), (et, edt), (em, edups) = (table_entries(x)
                                                    for x in (base, ours, theirs, merged))
    # keys already duplicated in an input carry inherited corruption. Their
    # parsed content is first-occurrence-only and not comparable, so only
    # presence is checked for them.
    eskip = edb | edo | edt
    for d in sorted(edups - edo - edt):
        viols.append("entry " + d + " is duplicated in the merge")

    def entry_both(k):
        if k in eskip:
            return
        if em[k][2] != 1:
            viols.append("entry %s has %d m_Localized fields" % (k, em[k][2]))
        _value_rule("entry %s text" % k, eb[k][0] if k in eb else None,
                    eo[k][0], et[k][0], em[k][0], viols)
        _set_rule("entry %s metadata" % k, eb[k][1] if k in eb else None,
                  eo[k][1], et[k][1], em[k][1], viols)

    def entry_copy(k, side):
        if k in eskip:
            return
        if em[k][0] != side[k][0] or em[k][1] != side[k][1]:
            viols.append("entry %s was altered while being added" % k)

    for k in sorted(set(eb) | set(eo) | set(et)):
        _presence_rule("entry " + k, k, eb, eo, et, em, viols, entry_both, entry_copy)
    (rb, rdb), (ro, rdo), (rt, rdt), (rm, rdups) = (refid_records(x)
                                                    for x in (base, ours, theirs, merged))
    rskip = rdb | rdo | rdt
    for d in sorted(rdups - rdo - rdt):
        viols.append("reference record " + d + " is duplicated with differing content")

    def rec_both(k):
        if k in rskip:
            return
        _value_rule("reference %s payload" % k, rb[k][0] if k in rb else None,
                    ro[k][0], rt[k][0], rm[k][0], viols)
        _set_rule("reference %s entry ids" % k, rb[k][1] if k in rb else None,
                  ro[k][1], rt[k][1], rm[k][1], viols)

    def rec_copy(k, side):
        if k in rskip:
            return
        if rm[k] != side[k]:
            viols.append("reference record %s was altered while being added" % k)

    for k in sorted(set(rb) | set(ro) | set(rt)):
        _presence_rule("reference record " + k, k, rb, ro, rt, rm, viols, rec_both, rec_copy)
    for k, (_loc, rids, _n) in sorted(em.items()):
        for r in sorted(rids):
            if not r.startswith("-") and r not in rm:
                viols.append("entry %s references rid %s which has no record" % (k, r))
    return viols


def text_merge(ours, base, theirs):
    """Plain 3-way text merge of the unwrapped inputs via git merge-file,
    used to recover after the native tool failed verification. Returns
    text and rc, or None and 1 when it can't run."""
    try:
        # stable -L labels: the defaults would leak meaningless temp paths
        t = subprocess.run(["git", "merge-file", "-p", "-L", "ours", "-L", "base",
                            "-L", "theirs", ours, base, theirs],
                           stdin=subprocess.DEVNULL, capture_output=True)
    except Exception:
        return None, 1
    if not t.stdout:
        return None, 1
    return t.stdout.decode("utf-8", "replace"), t.returncode


def conflict_file(ours_text, theirs_text):
    """A whole-file ours/theirs conflict, for when no automatic result can
    be verified. On driver failure git leaves the driver's output as the
    working-tree file. A marker-less file looks resolved and gets
    committed, so the output itself must be unmistakable."""
    if not ours_text.endswith("\n"):
        ours_text += "\n"
    if not theirs_text.endswith("\n"):
        theirs_text += "\n"
    return "<<<<<<< ours\n" + ours_text + "=======\n" + theirs_text + ">>>>>>> theirs\n"


# --- the merge driver --------------------------------------------------------------------------

def find_tool():
    t = os.environ.get("UNITY_YAML_MERGE")
    if t and os.path.exists(t):
        return t
    found = sorted(glob.glob(os.path.expanduser(
        "~/Unity/Hub/Editor/*/Editor/Data/Tools/UnityYAMLMerge")))
    return found[-1] if found else None


def _read(p):
    return open(p, encoding="utf-8", newline="").read()


def _write(p, t):
    with open(p, "w", encoding="utf-8", newline="") as f:
        f.write(t)


def merge(base, remote, local, output):
    """Unwrap inputs, run UnityYAMLMerge, rewrap and restore line endings into `output`.
    Returns the tool's exit code (0 = clean). Degrades to a plain native merge on any error."""
    tool = find_tool()
    if not tool:
        sys.exit("UnityYAMLMerge not found (set UNITY_YAML_MERGE)")
    d = tempfile.mkdtemp(prefix="uymf_")
    try:
        ub, ur, ul, uo = (os.path.join(d, n) for n in ("base", "remote", "local", "out"))
        unwrapped = True
        try:
            _write(ub, reserialize(_read(base), INF, INF, fix_empty=False))
            _write(ur, reserialize(_read(remote), INF, INF, fix_empty=False))
            _write(ul, reserialize(_read(local), INF, INF, fix_empty=False))
        except Exception:
            ub, ur, ul = base, remote, local        # unwrap failed: feed originals
            unwrapped = False
        shutil.copyfile(ul, uo)                       # tool writes the result to the 4th arg
        # -h headless, -p premerge, --force handles extension-less temp files. Omitting
        # --nomappinginoneline keeps flow mappings on one line (serializeInlineMappingsOnOneLine).
        r = subprocess.run([tool, "merge", "-h", "-p", "--force", ub, ur, ul, uo],
                           cwd=os.path.dirname(tool), stdin=subprocess.DEVNULL,
                           capture_output=True, env=dict(os.environ, TMP=d, TEMP=d))
        if not os.path.exists(uo):
            return r.returncode or 1
        try:
            result = reserialize(_read(uo))
        except Exception:
            result = _read(uo)                        # rewrap failed: raw merge output
        rc = r.returncode
        result = dedup_refids(result)

        def verify(text):
            # verified only when the inputs unwrapped: verification compares
            # unwrapped forms, so undecodable inputs can't be checked
            if not unwrapped:
                return []
            try:
                return validate_merge(_read(ub), _read(ul), _read(ur),
                                      reserialize(text, INF, INF, fix_empty=False))
            except Exception as e:
                return ["verification itself failed: %r" % e]

        viols = verify(result)
        if viols:
            # The native result is not a faithful 3-way merge. Retry as a
            # plain text merge and accept it only if it verifies. Otherwise
            # fail with markers. Never exit 0 on an unverified file.
            sys.stderr.write("UnityYAMLMerge-Fix: native merge failed verification:\n"
                             + "".join("  - " + v + "\n" for v in viols[:8]))
            healed, hrc = text_merge(ul, ub, ur)
            if healed is not None and hrc != 0:
                result, rc = healed, hrc
                sys.stderr.write("UnityYAMLMerge-Fix: conflict markers left; "
                                 "resolve by hand.\n")
            elif healed is not None and not verify(healed):
                try:
                    result = reserialize(healed)
                except Exception:
                    result = healed
                rc = 0
                sys.stderr.write("UnityYAMLMerge-Fix: recovered by text merge.\n")
            else:
                result, rc = conflict_file(_read(local), _read(remote)), 1
                sys.stderr.write("UnityYAMLMerge-Fix: whole-file conflict left; "
                                 "resolve by hand.\n")
        original = _read(local)                       # restore CRLF if the file was CRLF
        if original.count("\r\n") * 2 > original.count("\n"):
            result = result.replace("\r\n", "\n").replace("\n", "\r\n")
        _write(output, result)
        return rc
    finally:
        shutil.rmtree(d, ignore_errors=True)


def main(argv):
    if len(argv) < 4:
        sys.exit("usage: unityyamlmerge_fix.py BASE REMOTE LOCAL OUTPUT")
    return merge(argv[0], argv[1], argv[2], argv[3])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
