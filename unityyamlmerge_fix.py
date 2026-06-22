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
        try:
            _write(ub, reserialize(_read(base), INF, INF, fix_empty=False))
            _write(ur, reserialize(_read(remote), INF, INF, fix_empty=False))
            _write(ul, reserialize(_read(local), INF, INF, fix_empty=False))
        except Exception:
            ub, ur, ul = base, remote, local        # unwrap failed: feed originals
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
        original = _read(local)                       # restore CRLF if the file was CRLF
        if original.count("\r\n") * 2 > original.count("\n"):
            result = result.replace("\r\n", "\n").replace("\n", "\r\n")
        _write(output, result)
        return r.returncode
    finally:
        shutil.rmtree(d, ignore_errors=True)


def main(argv):
    if len(argv) < 4:
        sys.exit("usage: unityyamlmerge_fix.py BASE REMOTE LOCAL OUTPUT")
    return merge(argv[0], argv[1], argv[2], argv[3])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
