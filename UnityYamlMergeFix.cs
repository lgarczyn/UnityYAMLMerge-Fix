// UnityYAMLMerge-Fix: a git merge driver that makes Unity's Smart Merge
// produce editor-identical output. C# port of unityyamlmerge_fix.py, compiled
// to one IL exe that runs on the Mono runtime bundled with every Unity
// editor. The driver needs nothing beyond the editor install it already
// requires for UnityYAMLMerge.
//
// It runs the native UnityYAMLMerge but unwraps the inputs first, so the
// standard-YAML parser can't strip fold-trailing whitespace. It then re-wraps
// the output exactly as the editor would and restores the line endings.
// Every result is verified against true 3-way semantics before the driver
// reports success. See the structural verification section.
//
// The Python file is the reference implementation. This port is
// byte-equivalent, verified over the asset corpus and replayed history
// merges. Keep the two in sync.
//
// Driver usage, see README: mono uymf.exe BASE REMOTE LOCAL OUTPUT

using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Text;
using System.Text.RegularExpressions;

internal static class UnityYamlMergeFix
{
    // A `key: value` mapping line. The leading group absorbs sequence dashes
    // like `- k: v` and nested `- - k: v`, so the dash counts toward the
    // indent and the value still folds. Group 3 keeps trailing spaces.
    private static readonly Regex Key = new Regex(@"^(\s*(?:- )*)([\w.\-/]+):\s(.+)$");
    // A bare plain scalar sequence item like `- Assembly.Type`, no `key:` form.
    private static readonly Regex Seq = new Regex(@"^(\s*)- (\S.*)$");
    private static readonly Regex Mappingish = new Regex(@"^[\w.\-/]+:(\s|$)");
    private const string ExcludeFirst = "|>{[&*!#%";      // value heads that are not plain scalars
    private static readonly Regex EmptyFlow = new Regex(@": ''(?=[,}])");

    private const int PlainWidth = 79;
    private const int QuotedWidth = 80;
    private const int Inf = 1000000000;                    // "unwrap" width: never fold

    // Byte-exact file IO: no newline translation, no BOM stripping or
    // emission. Invalid UTF-8 throws and the caller's catch then degrades
    // gracefully, like Python's strict codec.
    private static readonly Encoding Utf8Strict = new UTF8Encoding(false, true);

    private static string ReadFile(string path)
    {
        return Utf8Strict.GetString(File.ReadAllBytes(path));
    }

    private static void WriteFile(string path, string text)
    {
        File.WriteAllBytes(path, Utf8Strict.GetBytes(text));
    }

    // Column widths count code points, not UTF-16 units, so astral chars
    // fold at the same points as in Python.
    private static int CpLen(string s)
    {
        int n = s.Length;
        foreach (char c in s)
            if (char.IsLowSurrogate(c))
                n--;
        return n;
    }

    private static string Sub(string s, int start)       // Python s[start:] never throws
    {
        return start >= s.Length ? "" : s.Substring(start);
    }

    // --- editor-faithful re-wrap codec -------------------------------------
    // Works on raw text, never decoding to a value: Unity's pre-fold
    // whitespace is significant, so the oracle is byte-equality. A plain
    // scalar breaks at the last space of a run past the width, keeping
    // earlier spaces as trailing whitespace. Quoted scalars fold at
    // best_width keeping significant space.

    private static List<string> ReemitPlain(string value, string prefix, string contIndent, int width)
    {
        var lines = new List<string>();
        var cur = new StringBuilder(prefix);
        int col = CpLen(prefix), n = value.Length;
        for (int i = 0; i < n; i++)
        {
            char c = value[i];
            if (c == ' ' && col > width && !(i + 1 < n && value[i + 1] == ' '))
            {
                lines.Add(cur.ToString());
                cur = new StringBuilder(contIndent);
                col = CpLen(contIndent);
            }
            else
            {
                cur.Append(c);
                if (!char.IsLowSurrogate(c))
                    col++;
            }
        }
        lines.Add(cur.ToString());
        return lines;
    }

    private static string JoinPlainValue(string val, List<string> conts, string contIndent)
    {
        // inverse of ReemitPlain: each continuation contributes one fold
        // space plus its content
        var sb = new StringBuilder(val);
        foreach (string c in conts)
            sb.Append(' ').Append(Sub(c, contIndent.Length));
        return sb.ToString();
    }

    private static List<string> GatherContinuations(string[] lines, int i, int keyIndent, out int j)
    {
        // strictly-more-indented non-blank lines that are not mappings
        var conts = new List<string>();
        j = i + 1;
        while (j < lines.Length)
        {
            string c = lines[j];
            int ci = c.Length - c.TrimStart(' ').Length;
            if (c.Trim().Length == 0 || ci <= keyIndent || Key.IsMatch(c))
                break;
            conts.Add(c);
            j++;
        }
        return conts;
    }

    private static List<string> GatherQuoted(string[] lines, int i, int quoteCol, char quote, out int j)
    {
        // span a quoted block to its matching close, delimited by the quote,
        // not indent. `''` and `\"` and `\\` are escapes, not closes. The
        // value may contain blank lines and `key:` prose.
        string s = string.Join("\n", lines, i, lines.Length - i);
        int k = quoteCol + 1, n = s.Length;
        while (k < n)
        {
            char c = s[k];
            if (quote == '\'')
            {
                if (c == '\'')
                {
                    if (k + 1 < n && s[k + 1] == '\'')
                    {
                        k += 2;
                        continue;
                    }
                    break;
                }
            }
            else
            {
                if (c == '\\')
                {
                    k += 2;
                    continue;
                }
                if (c == '"')
                    break;
            }
            k++;
        }
        int newlines = 0;
        for (int t = 0, end = Math.Min(k + 1, n); t < end; t++)
            if (s[t] == '\n')
                newlines++;
        j = Math.Min(i + newlines + 1, lines.Length);
        var block = new List<string>();
        for (int t = i; t < j; t++)
            block.Add(lines[t]);
        return block;
    }

    private static List<string> ReemitQuoted(string content, string prefix, int contIndent, int width)
    {
        // single-quoted: ' -> ''. A content newline emits a blank line. The
        // column accumulates across newlines, first +cont+2, each extra +1.
        // A fold never splits a space run. A trailing \n closes alone.
        if (content == "")
            return new List<string> { prefix + "'" };
        string[] segments = content.Replace("'", "''").Split('\n');
        var lines = new List<string>();
        int vcol = CpLen(prefix);
        string ind = new string(' ', contIndent);
        bool prevEmpty = false;
        for (int s = 0; s < segments.Length; s++)
        {
            string seg = segments[s];
            if (s > 0)
            {
                lines.Add("");
                vcol += prevEmpty ? 1 : contIndent + 2;
            }
            if (seg == "")
            {
                if (s == 0)
                    lines.Add(prefix);
                prevEmpty = s != 0;
                continue;
            }
            prevEmpty = false;
            var cur = new StringBuilder(s == 0 ? prefix : ind);
            bool firstWord = true;
            foreach (string word in seg.Split(' '))
            {
                if (firstWord)
                {
                    cur.Append(word);
                    vcol += CpLen(word);
                    firstWord = false;
                }
                else if (word != "" && vcol + 1 > width)
                {
                    lines.Add(cur.ToString());
                    cur = new StringBuilder(ind).Append(word);
                    vcol = contIndent + CpLen(word);
                }
                else
                {
                    cur.Append(' ').Append(word);
                    vcol += 1 + CpLen(word);
                }
            }
            lines.Add(cur.ToString());
        }
        if (segments[segments.Length - 1] == "")
            lines.Add("'");
        else
            lines[lines.Count - 1] += "'";
        return lines;
    }

    private static string CutAtLastQuote(string line, char quote)
    {
        // Python `line[:line.rfind(q)]`: rfind returning -1 silently drops
        // the last char instead
        int q = line.LastIndexOf(quote);
        if (q >= 0)
            return line.Substring(0, q);
        return line.Length > 0 ? line.Substring(0, line.Length - 1) : line;
    }

    private static string DecodeQuoted(List<string> block, string prefix, int contIndent)
    {
        // inverse of ReemitQuoted: a soft fold restores one space and a
        // blank line restores one newline
        var body = new List<string>(block.GetRange(0, block.Count - 1));
        string last = block[block.Count - 1];
        if (last.Trim() != "'")
            body.Add(CutAtLastQuote(last, '\''));
        var phys = new List<string> { Sub(body[0], prefix.Length) };
        for (int t = 1; t < body.Count; t++)
            phys.Add(body[t].Trim().Length == 0 ? "" : Sub(body[t], contIndent));
        var content = new StringBuilder(phys[0]);
        for (int k = 1; k < phys.Count; k++)
        {
            if (phys[k] == "")
                content.Append('\n');
            else if (phys[k - 1] == "")
                content.Append(phys[k]);
            else
                content.Append(' ').Append(phys[k]);
        }
        return content.ToString().Replace("''", "'");
    }

    private static List<string> ReemitDouble(string escaped, string prefix, int contIndent, int width)
    {
        // double-quoted is one continuous escaped flow, escapes like \n and
        // \uXXXX are spaceless, so just re-wrap
        var lines = new List<string>();
        var cur = new StringBuilder(prefix);
        int vcol = CpLen(prefix);
        string ind = new string(' ', contIndent);
        bool firstWord = true;
        foreach (string word in escaped.Split(' '))
        {
            if (firstWord)
            {
                cur.Append(word);
                vcol += CpLen(word);
                firstWord = false;
            }
            else if (word != "" && vcol + 1 > width)
            {
                lines.Add(cur.ToString());
                cur = new StringBuilder(ind).Append(word);
                vcol = contIndent + CpLen(word);
            }
            else
            {
                cur.Append(' ').Append(word);
                vcol += 1 + CpLen(word);
            }
        }
        lines.Add(cur.Append('"').ToString());
        return lines;
    }

    private static string DecodeDouble(List<string> block, string prefix, int contIndent)
    {
        // soft folds rejoined with one space each. The `\ ` escaped space
        // becomes a literal space, everything else is verbatim.
        var body = new List<string>(block.GetRange(0, block.Count - 1));
        body.Add(CutAtLastQuote(block[block.Count - 1], '"'));
        var parts = new List<string> { Sub(body[0], prefix.Length) };
        for (int t = 1; t < body.Count; t++)
            parts.Add(Sub(body[t], contIndent));
        string s = string.Join(" ", parts);
        var result = new StringBuilder();
        int i = 0, n = s.Length;
        while (i < n)
        {
            if (s[i] == '\\' && i + 1 < n)
            {
                if (s[i + 1] == ' ')
                    result.Append(' ');
                else
                    result.Append(s[i]).Append(s[i + 1]);
                i += 2;
            }
            else
            {
                result.Append(s[i]);
                i++;
            }
        }
        return result.ToString();
    }

    /// Re-wrap `text` the way the Unity editor would. Plain scalars fold at
    /// `width`, quoted ones at `quotedWidth`. Use Inf to unwrap. Quoted
    /// blocks with mixed LF/CRLF pass through verbatim, since a terminator
    /// is never invented. Idempotent on editor-form input. Fold lines
    /// inherit the block's first-line terminator. With fixEmpty, injected
    /// `''` flow values are stripped.
    private static string Reserialize(string text, int width = PlainWidth,
                                      int quotedWidth = QuotedWidth, bool fixEmpty = true)
    {
        // content plus hadCr per line, preserving each terminator, because
        // some assets carry mixed LF and CRLF
        string[] raw = text.Split('\n');
        int n = raw.Length;
        var lines = new string[n];
        var crs = new bool[n];
        for (int t = 0; t < n; t++)
        {
            bool cr = raw[t].EndsWith("\r", StringComparison.Ordinal);
            lines[t] = cr ? raw[t].Substring(0, raw[t].Length - 1) : raw[t];
            crs[t] = cr;
        }
        var outLines = new List<string>();
        var outCrs = new List<bool>();
        int i = 0;
        while (i < n)
        {
            string line = lines[i];
            Match m = Key.Match(line);
            if (!m.Success)
            {
                Match sm = Seq.Match(line);
                string bare = sm.Success ? sm.Groups[2].Value : null;
                if (sm.Success && ExcludeFirst.IndexOf(bare[0]) < 0 && bare[0] != '\'' && bare[0] != '"'
                    && !bare.StartsWith("- ", StringComparison.Ordinal) && !Mappingish.IsMatch(bare))
                {
                    int j;
                    var conts = GatherContinuations(lines, i, sm.Groups[1].Value.Length, out j);
                    string prefix = line.Substring(0, line.Length - bare.Length);
                    string contIndent = conts.Count > 0
                        ? new string(' ', conts[0].Length - conts[0].TrimStart(' ').Length)
                        : new string(' ', prefix.Length);
                    string value = JoinPlainValue(bare, conts, contIndent);
                    foreach (string e in ReemitPlain(value, prefix, contIndent, width))
                    {
                        outLines.Add(e);
                        outCrs.Add(crs[i]);
                    }
                    i = j;
                    continue;
                }
                outLines.Add(line);
                outCrs.Add(crs[i]);
                i++;
                continue;
            }
            string indent = m.Groups[1].Value, val = m.Groups[3].Value;
            char first = val[0];
            if (ExcludeFirst.IndexOf(first) >= 0)
            {
                outLines.Add(line);
                outCrs.Add(crs[i]);
                i++;
                continue;
            }
            if (first == '\'' || first == '"')
            {
                int j;
                var block = GatherQuoted(lines, i, line.Length - val.Length, first, out j);
                bool uniformCr = true;
                for (int t = i + 1; t < j; t++)
                    if (crs[t] != crs[i])
                    {
                        uniformCr = false;
                        break;
                    }
                if (uniformCr)
                {
                    string qp = line.Substring(0, line.Length - val.Length + 1);
                    var inner = new List<string>();
                    for (int t = 1; t < block.Count - 1; t++)
                        if (block[t].Trim().Length != 0)
                            inner.Add(block[t]);
                    int ci = inner.Count > 0
                        ? inner[0].Length - inner[0].TrimStart(' ').Length
                        : indent.Length + 2;
                    string decoded = first == '\''
                        ? DecodeQuoted(block, qp, ci)
                        : DecodeDouble(block, qp, ci);
                    var re = first == '\''
                        ? ReemitQuoted(decoded, qp, ci, quotedWidth)
                        : ReemitDouble(decoded, qp, ci, quotedWidth);
                    foreach (string e in re)
                    {
                        outLines.Add(e);
                        outCrs.Add(crs[i]);
                    }
                }
                else
                {
                    for (int t = i; t < j; t++)
                    {
                        outLines.Add(lines[t]);
                        outCrs.Add(crs[t]);
                    }
                }
                i = j;
                continue;
            }
            {
                int j;
                var conts = GatherContinuations(lines, i, indent.Length, out j);
                string prefix = line.Substring(0, line.Length - val.Length);
                string contIndent = conts.Count > 0
                    ? new string(' ', conts[0].Length - conts[0].TrimStart(' ').Length)
                    : new string(' ', indent.Length + 2);
                string value = JoinPlainValue(val, conts, contIndent);
                foreach (string e in ReemitPlain(value, prefix, contIndent, width))
                {
                    outLines.Add(e);
                    outCrs.Add(crs[i]);
                }
                i = j;
            }
        }
        var sb = new StringBuilder();
        for (int t = 0; t < outLines.Count; t++)
        {
            if (t > 0)
                sb.Append('\n');
            sb.Append(outLines[t]);
            if (outCrs[t])
                sb.Append('\r');
        }
        string result = sb.ToString();
        return fixEmpty ? EmptyFlow.Replace(result, ": ") : result;
    }

    // --- structural verification --------------------------------------------
    // UnityYAMLMerge is line-based: dense edits near long multi-line scalars
    // can drop or misalign records and still exit 0. Everything id-keyed is
    // therefore verified against true 3-way semantics on the unwrapped forms.
    // String-table entries are keyed by m_Id, text and metadata rid set.
    // SerializeReference records are keyed by rid, payload and m_SharedEntries
    // id set. Whole YAML documents are keyed by their &anchor. A clean exit is
    // a verified merge. Any deviation is escalated: healed by a validated text
    // merge when possible, otherwise the merge fails with conflict markers.
    // Git keeps the driver's output on failure, and a clean-looking lossy
    // file gets committed as-is.

    private static readonly Regex EntryId = new Regex(@"^  - m_Id: (\d+)\s*$");
    private static readonly Regex RefRid = new Regex(@"^    - rid: (-?\d+)\s*$");
    private static readonly Regex ItemRid = new Regex(@"^\s+- rid: (-?\d+)$");
    private static readonly Regex SharedId = new Regex(@"^\s+- id: (-?\d+)$");
    private static readonly Regex DocAnchor = new Regex(@"^--- !u!\d+ &(-?\d+)", RegexOptions.Multiline);

    private sealed class TableEntry
    {
        public string Loc;
        public HashSet<string> Rids;
        public int N;

        public bool SameAs(TableEntry other)
        {
            return other != null && Loc == other.Loc && N == other.N && Rids.SetEquals(other.Rids);
        }
    }

    private sealed class RefRec
    {
        public string Raw;
        public HashSet<string> Ids;

        public bool SameAs(RefRec other)
        {
            return other != null && Raw == other.Raw && Ids.SetEquals(other.Ids);
        }
    }

    private static bool IsSectionKey(string line)      // a 2-indent `key:` line
    {
        return line.StartsWith("  ", StringComparison.Ordinal)
            && !line.StartsWith("  -", StringComparison.Ordinal)
            && line.Length > 2 && line.Substring(2, 1).Trim().Length != 0;
    }

    private static Dictionary<string, TableEntry> ParseTableEntries(string text,
                                                                    HashSet<string> dups)
    {
        var entries = new Dictionary<string, TableEntry>();
        bool inTable = false;
        TableEntry cur = null;
        string field = null;
        foreach (string line in text.Split('\n'))
        {
            if (IsSectionKey(line))
            {
                inTable = line.Trim() == "m_TableData:";   // any other 2-indent key ends the table
                cur = null;
                continue;
            }
            if (!inTable)
                continue;
            Match m = EntryId.Match(line);
            if (m.Success)
            {
                string eid = m.Groups[1].Value;
                if (entries.ContainsKey(eid))
                    dups.Add(eid);
                else
                    entries[eid] = new TableEntry { Loc = "", Rids = new HashSet<string>(), N = 0 };
                cur = entries[eid];
                field = null;
                continue;
            }
            if (cur == null)
                continue;
            if (line.StartsWith("    m_Localized:", StringComparison.Ordinal))
            {
                cur.Loc = cur.N == 0 ? line.Substring(16) : cur.Loc + "\n" + line.Substring(16);
                cur.N++;
                field = "loc";
            }
            else if (line.StartsWith("    m_", StringComparison.Ordinal))
            {
                field = line.StartsWith("    m_Metadata:", StringComparison.Ordinal) ? "meta" : null;
            }
            else if (field == "meta" && ItemRid.IsMatch(line))
            {
                cur.Rids.Add(ItemRid.Match(line).Groups[1].Value);
            }
            else if (field == "loc")                       // continuation of an unwrapped value
            {
                cur.Loc += "\n" + line;
            }
        }
        return entries;
    }

    private static Dictionary<string, RefRec> ParseRefIds(string text, HashSet<string> dups)
    {
        var recs = new Dictionary<string, RefRec>();
        var raws = new Dictionary<string, List<string>>();
        bool inRefs = false, inList = false;
        RefRec cur = null;
        List<string> curRaw = null;
        foreach (string line in text.Split('\n'))
        {
            if (IsSectionKey(line))
            {
                inRefs = line.Trim() == "references:";
                inList = false;
                cur = null;
                continue;
            }
            if (!inRefs)
                continue;
            if (line == "    RefIds:")
            {
                inList = true;
                continue;
            }
            if (!inList)
                continue;
            Match m = RefRid.Match(line);
            if (m.Success)
            {
                string rid = m.Groups[1].Value;
                if (recs.ContainsKey(rid))
                    dups.Add(rid);
                else
                {
                    recs[rid] = new RefRec { Raw = "", Ids = new HashSet<string>() };
                    raws[rid] = new List<string>();
                }
                cur = recs[rid];
                curRaw = raws[rid];
                continue;
            }
            if (cur != null && (line.StartsWith("      ", StringComparison.Ordinal) || line == ""))
            {
                Match sm = SharedId.Match(line);
                if (sm.Success)
                    cur.Ids.Add(sm.Groups[1].Value);
                else
                    curRaw.Add(line);
            }
            else
            {
                cur = null;
            }
        }
        foreach (var kv in raws)
            recs[kv.Key].Raw = string.Join("\n", kv.Value);
        return recs;
    }

    /// Drop byte-identical duplicate RefIds records, a known benign churn of
    /// the native merge. Duplicates that differ are left for verification to
    /// reject.
    private static string DedupRefids(string text)
    {
        string[] lines = text.Split('\n');
        var kept = new List<string>();
        var seen = new Dictionary<string, string>();
        bool inRefs = false, inList = false;
        int i = 0, n = lines.Length;
        while (i < n)
        {
            string line = lines[i];
            if (IsSectionKey(line))
            {
                inRefs = line.Trim() == "references:";
                inList = false;
            }
            else if (inRefs && line == "    RefIds:")
            {
                inList = true;
            }
            Match m = inList ? RefRid.Match(line) : Match.Empty;
            if (!m.Success)
            {
                kept.Add(line);
                i++;
                continue;
            }
            int j = i + 1;
            while (j < n && lines[j].StartsWith("      ", StringComparison.Ordinal))
                j++;
            string block = string.Join("\n", lines, i, j - i);
            string rid = m.Groups[1].Value;
            string prior;
            if (seen.TryGetValue(rid, out prior) && prior == block)
            {
                i = j;                                     // identical duplicate: drop it
                continue;
            }
            if (!seen.ContainsKey(rid))
                seen[rid] = block;
            for (int t = i; t < j; t++)
                kept.Add(lines[t]);
            i = j;
        }
        return string.Join("\n", kept);
    }

    private static void ValueRule(string what, string b, string o, string t, string m,
                                  List<string> viols)
    {
        // scalar 3-way: a silent side-pick or an invented value is as much
        // a loss as a drop
        if (o == t)
        {
            if (m != o)
                viols.Add(what + " matches neither side");
        }
        else if (b == o)
        {
            if (m != t)
                viols.Add(what + " lost theirs' change");
        }
        else if (b == t)
        {
            if (m != o)
                viols.Add(what + " lost ours' change");
        }
        else
        {
            viols.Add(what + " changed differently on both sides");
        }
    }

    private static void SetRule(string what, HashSet<string> b, HashSet<string> o,
                                HashSet<string> t, HashSet<string> m, List<string> viols)
    {
        // id sets merge as sets: both sides' additions and removals must
        // all be honored
        if (b == null)
            b = new HashSet<string>();
        var addO = Minus(o, b);
        var addT = Minus(t, b);
        var remO = Minus(b, o);
        var remT = Minus(b, t);
        if (addO.Overlaps(remT) || addT.Overlaps(remO))
        {
            viols.Add(what + " has an add/remove conflict");
            return;
        }
        var expect = new HashSet<string>(b);
        expect.IntersectWith(o);
        expect.IntersectWith(t);
        expect.UnionWith(addO);
        expect.UnionWith(addT);
        if (!m.SetEquals(expect))
            viols.Add(what + " does not match the 3-way id set");
    }

    private static HashSet<string> Minus(HashSet<string> a, HashSet<string> b)
    {
        var r = new HashSet<string>(a);
        r.ExceptWith(b);
        return r;
    }

    private static void PresenceRule<T>(string what, string key,
                                        Dictionary<string, T> b, Dictionary<string, T> o,
                                        Dictionary<string, T> t, Dictionary<string, T> m,
                                        List<string> viols, Func<T, T, bool> same,
                                        Action<string> verifyBoth,
                                        Action<string, Dictionary<string, T>> verifyCopy)
        where T : class
    {
        bool inO = o.ContainsKey(key), inT = t.ContainsKey(key), inM = m.ContainsKey(key);
        if (inO && inT)
        {
            if (!inM)
                viols.Add(what + " was dropped (present on both sides)");
            else
                verifyBoth(key);
        }
        else if (inO || inT)
        {
            var side = inO ? o : t;
            string other = inO ? "theirs" : "ours";
            if (!b.ContainsKey(key))
            {
                if (!inM)
                    viols.Add(what + " added on one side was dropped");
                else
                    verifyCopy(key, side);
            }
            else if (same(side[key], b[key]))
            {
                if (inM)
                    viols.Add(what + " deleted on " + other + " was resurrected");
            }
            else
            {
                viols.Add(what + " edited on one side but deleted on " + other);
            }
        }
        else if (inM && b.ContainsKey(key))
        {
            viols.Add(what + " deleted on both sides was resurrected");
        }
    }

    private static IEnumerable<string> SortedKeys<T>(params Dictionary<string, T>[] dicts)
    {
        var keys = new HashSet<string>();
        foreach (var d in dicts)
            keys.UnionWith(d.Keys);
        var list = new List<string>(keys);
        list.Sort(StringComparer.Ordinal);
        return list;
    }

    /// Every violation of faithful 3-way merge semantics. Empty means verified.
    private static List<string> ValidateMerge(string baseText, string ours, string theirs,
                                              string merged)
    {
        baseText = baseText.Replace("\r\n", "\n");
        ours = ours.Replace("\r\n", "\n");
        theirs = theirs.Replace("\r\n", "\n");
        merged = merged.Replace("\r\n", "\n");
        var viols = new List<string>();

        var ab = Anchors(baseText);
        var ao = Anchors(ours);
        var at = Anchors(theirs);
        var am = Anchors(merged);
        var both = new HashSet<string>(ao);
        both.IntersectWith(at);
        both.ExceptWith(am);
        var bothList = new List<string>(both);
        bothList.Sort(StringComparer.Ordinal);
        foreach (var a in bothList)
            viols.Add("document &" + a + " was dropped (present on both sides)");
        var added = new HashSet<string>(ao);
        added.UnionWith(at);
        added.ExceptWith(ab);
        added.ExceptWith(am);
        added.ExceptWith(both);
        var addedList = new List<string>(added);
        addedList.Sort(StringComparer.Ordinal);
        foreach (var a in addedList)
            viols.Add("document &" + a + " added on one side was dropped");

        var edb = new HashSet<string>();
        var edo = new HashSet<string>();
        var edt = new HashSet<string>();
        var edups = new HashSet<string>();
        var eb = ParseTableEntries(baseText, edb);
        var eo = ParseTableEntries(ours, edo);
        var et = ParseTableEntries(theirs, edt);
        var em = ParseTableEntries(merged, edups);
        // keys already duplicated in an input carry inherited corruption.
        // Their parsed content is first-occurrence-only and not comparable,
        // so only presence is checked for them.
        var eskip = new HashSet<string>(edb);
        eskip.UnionWith(edo);
        eskip.UnionWith(edt);
        edups.ExceptWith(edo);
        edups.ExceptWith(edt);
        var edupList = new List<string>(edups);
        edupList.Sort(StringComparer.Ordinal);
        foreach (var d in edupList)
            viols.Add("entry " + d + " is duplicated in the merge");

        Action<string> entryBoth = k =>
        {
            if (eskip.Contains(k))
                return;
            if (em[k].N != 1)
                viols.Add("entry " + k + " has " + em[k].N + " m_Localized fields");
            ValueRule("entry " + k + " text", eb.ContainsKey(k) ? eb[k].Loc : null,
                      eo[k].Loc, et[k].Loc, em[k].Loc, viols);
            SetRule("entry " + k + " metadata", eb.ContainsKey(k) ? eb[k].Rids : null,
                    eo[k].Rids, et[k].Rids, em[k].Rids, viols);
        };
        Action<string, Dictionary<string, TableEntry>> entryCopy = (k, side) =>
        {
            if (eskip.Contains(k))
                return;
            if (em[k].Loc != side[k].Loc || !em[k].Rids.SetEquals(side[k].Rids))
                viols.Add("entry " + k + " was altered while being added");
        };
        foreach (var k in SortedKeys(eb, eo, et))
            PresenceRule("entry " + k, k, eb, eo, et, em, viols,
                         (x, y) => x.SameAs(y), entryBoth, entryCopy);

        var rdb = new HashSet<string>();
        var rdo = new HashSet<string>();
        var rdt = new HashSet<string>();
        var rdups = new HashSet<string>();
        var rb = ParseRefIds(baseText, rdb);
        var ro = ParseRefIds(ours, rdo);
        var rt = ParseRefIds(theirs, rdt);
        var rm = ParseRefIds(merged, rdups);
        var rskip = new HashSet<string>(rdb);
        rskip.UnionWith(rdo);
        rskip.UnionWith(rdt);
        rdups.ExceptWith(rdo);
        rdups.ExceptWith(rdt);
        var rdupList = new List<string>(rdups);
        rdupList.Sort(StringComparer.Ordinal);
        foreach (var d in rdupList)
            viols.Add("reference record " + d + " is duplicated with differing content");

        Action<string> recBoth = k =>
        {
            if (rskip.Contains(k))
                return;
            ValueRule("reference " + k + " payload", rb.ContainsKey(k) ? rb[k].Raw : null,
                      ro[k].Raw, rt[k].Raw, rm[k].Raw, viols);
            SetRule("reference " + k + " entry ids", rb.ContainsKey(k) ? rb[k].Ids : null,
                    ro[k].Ids, rt[k].Ids, rm[k].Ids, viols);
        };
        Action<string, Dictionary<string, RefRec>> recCopy = (k, side) =>
        {
            if (rskip.Contains(k))
                return;
            if (!rm[k].SameAs(side[k]))
                viols.Add("reference record " + k + " was altered while being added");
        };
        foreach (var k in SortedKeys(rb, ro, rt))
            PresenceRule("reference record " + k, k, rb, ro, rt, rm, viols,
                         (x, y) => x.SameAs(y), recBoth, recCopy);

        foreach (var k in SortedKeys(em))
        {
            var rids = new List<string>(em[k].Rids);
            rids.Sort(StringComparer.Ordinal);
            foreach (var r in rids)
                if (!r.StartsWith("-", StringComparison.Ordinal) && !rm.ContainsKey(r))
                    viols.Add("entry " + k + " references rid " + r + " which has no record");
        }
        return viols;
    }

    private static HashSet<string> Anchors(string text)
    {
        var set = new HashSet<string>();
        foreach (Match m in DocAnchor.Matches(text))
            set.Add(m.Groups[1].Value);
        return set;
    }

    /// Plain 3-way text merge of the unwrapped inputs via git merge-file,
    /// used to recover after the native tool failed verification. Returns
    /// null with rc 1 when it can't run.
    private static string TextMerge(string ours, string basePath, string theirs, out int rc)
    {
        byte[] stdout;
        try
        {
            // stable -L labels: the defaults would leak meaningless temp paths
            rc = RunProcess("git", new[] { "merge-file", "-p", "-L", "ours", "-L", "base",
                                           "-L", "theirs", ours, basePath, theirs },
                            null, null, out stdout);
        }
        catch (Exception)
        {
            rc = 1;
            return null;
        }
        if (stdout == null || stdout.Length == 0)
        {
            rc = 1;
            return null;
        }
        return Encoding.UTF8.GetString(stdout);
    }

    /// A whole-file ours/theirs conflict, for when no automatic result can
    /// be verified. On driver failure git leaves the driver's output as the
    /// working-tree file. A marker-less file looks resolved and gets
    /// committed, so the output itself must be unmistakable.
    private static string ConflictFile(string oursText, string theirsText)
    {
        if (!oursText.EndsWith("\n", StringComparison.Ordinal))
            oursText += "\n";
        if (!theirsText.EndsWith("\n", StringComparison.Ordinal))
            theirsText += "\n";
        return "<<<<<<< ours\n" + oursText + "=======\n" + theirsText + ">>>>>>> theirs\n";
    }

    // --- the merge driver ----------------------------------------------------

    private static string FindTool()
    {
        string t = Environment.GetEnvironmentVariable("UNITY_YAML_MERGE");
        if (!string.IsNullOrEmpty(t) && (File.Exists(t) || Directory.Exists(t)))
            return t;
        string hub = Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.UserProfile),
            "Unity", "Hub", "Editor");
        var found = new List<string>();
        if (Directory.Exists(hub))
            foreach (string v in Directory.GetDirectories(hub))
            {
                string p = Path.Combine(v, "Editor", "Data", "Tools", "UnityYAMLMerge");
                if (File.Exists(p))
                    found.Add(p);
            }
        found.Sort(StringComparer.Ordinal);
        return found.Count > 0 ? found[found.Count - 1] : null;
    }

    private static string QuoteArg(string a)
    {
        if (a.Length > 0 && a.IndexOfAny(new[] { ' ', '\t', '"' }) < 0)
            return a;
        var sb = new StringBuilder("\"");
        int backslashes = 0;
        foreach (char c in a)
        {
            if (c == '\\')
            {
                backslashes++;
            }
            else if (c == '"')
            {
                sb.Append('\\', backslashes * 2 + 1).Append('"');
                backslashes = 0;
            }
            else
            {
                sb.Append('\\', backslashes).Append(c);
                backslashes = 0;
            }
        }
        return sb.Append('\\', backslashes * 2).Append('"').ToString();
    }

    private static int RunProcess(string exe, string[] args, string cwd,
                                  string tempDir, out byte[] stdout)
    {
        var quoted = new string[args.Length];
        for (int t = 0; t < args.Length; t++)
            quoted[t] = QuoteArg(args[t]);
        var psi = new ProcessStartInfo
        {
            FileName = exe,
            Arguments = string.Join(" ", quoted),
            UseShellExecute = false,
            RedirectStandardInput = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
        };
        if (cwd != null)
            psi.WorkingDirectory = cwd;
        if (tempDir != null)
        {
            psi.EnvironmentVariables["TMP"] = tempDir;
            psi.EnvironmentVariables["TEMP"] = tempDir;
        }
        using (var p = Process.Start(psi))
        {
            p.StandardInput.Close();
            var outBuf = new MemoryStream();
            var errDone = System.Threading.Tasks.Task.Run(() =>
                p.StandardError.BaseStream.CopyTo(Stream.Null));
            p.StandardOutput.BaseStream.CopyTo(outBuf);
            errDone.Wait();
            p.WaitForExit();
            stdout = outBuf.ToArray();
            return p.ExitCode;
        }
    }

    /// Unwrap inputs, run UnityYAMLMerge, rewrap and restore line endings
    /// into `output`. Returns the tool's exit code, 0 means clean. Degrades
    /// to a plain native merge on any error.
    private static int Merge(string basePath, string remote, string local, string output)
    {
        string tool = FindTool();
        if (tool == null)
        {
            Console.Error.WriteLine("UnityYAMLMerge not found (set UNITY_YAML_MERGE)");
            return 1;
        }
        string dir = Path.Combine(Path.GetTempPath(),
                                  "uymf_" + Path.GetRandomFileName().Replace(".", ""));
        Directory.CreateDirectory(dir);
        try
        {
            string ub = Path.Combine(dir, "base"), ur = Path.Combine(dir, "remote");
            string ul = Path.Combine(dir, "local"), uo = Path.Combine(dir, "out");
            bool unwrapped = true;
            try
            {
                WriteFile(ub, Reserialize(ReadFile(basePath), Inf, Inf, false));
                WriteFile(ur, Reserialize(ReadFile(remote), Inf, Inf, false));
                WriteFile(ul, Reserialize(ReadFile(local), Inf, Inf, false));
            }
            catch (Exception)
            {
                ub = basePath;                             // unwrap failed: feed originals
                ur = remote;
                ul = local;
                unwrapped = false;
            }
            File.Copy(ul, uo, true);                       // tool writes the result to the 4th arg
            // -h headless, -p premerge, --force handles extension-less temp
            // files. Omitting --nomappinginoneline keeps flow mappings on
            // one line, matching serializeInlineMappingsOnOneLine.
            byte[] ignored;
            int rc = RunProcess(tool, new[] { "merge", "-h", "-p", "--force", ub, ur, ul, uo },
                                Path.GetDirectoryName(tool), dir, out ignored);
            if (!File.Exists(uo))
                return rc != 0 ? rc : 1;
            string result;
            try
            {
                result = Reserialize(ReadFile(uo));
            }
            catch (Exception)
            {
                result = ReadFile(uo);                     // rewrap failed: raw merge output
            }
            result = DedupRefids(result);
            Func<string, List<string>> verify = text =>
            {
                // verified only when the inputs unwrapped: verification
                // compares unwrapped forms, so undecodable inputs can't be
                // checked
                if (!unwrapped)
                    return new List<string>();
                try
                {
                    return ValidateMerge(ReadFile(ub), ReadFile(ul), ReadFile(ur),
                                         Reserialize(text, Inf, Inf, false));
                }
                catch (Exception e)
                {
                    return new List<string> { "verification itself failed: " + e.Message };
                }
            };
            var viols = verify(result);
            if (viols.Count > 0)
            {
                // The native result is not a faithful 3-way merge. Retry as
                // a plain text merge and accept it only if it verifies.
                // Otherwise fail with markers. Never exit 0 on an unverified
                // file.
                var msg = new StringBuilder("UnityYAMLMerge-Fix: native merge failed verification:\n");
                for (int t = 0; t < viols.Count && t < 8; t++)
                    msg.Append("  - ").Append(viols[t]).Append('\n');
                Console.Error.Write(msg.ToString());
                int hrc;
                string healed = TextMerge(ul, ub, ur, out hrc);
                if (healed != null && hrc != 0)
                {
                    result = healed;
                    rc = hrc;
                    Console.Error.Write("UnityYAMLMerge-Fix: conflict markers left; "
                                        + "resolve by hand.\n");
                }
                else if (healed != null && verify(healed).Count == 0)
                {
                    try
                    {
                        result = Reserialize(healed);
                    }
                    catch (Exception)
                    {
                        result = healed;
                    }
                    rc = 0;
                    Console.Error.Write("UnityYAMLMerge-Fix: recovered by text merge.\n");
                }
                else
                {
                    result = ConflictFile(ReadFile(local), ReadFile(remote));
                    rc = 1;
                    Console.Error.Write("UnityYAMLMerge-Fix: whole-file conflict left; "
                                        + "resolve by hand.\n");
                }
            }
            string original = ReadFile(local);             // restore CRLF if the file was CRLF
            int crlfCount = CountOf(original, "\r\n");
            if (crlfCount * 2 > CountOf(original, "\n"))
                result = result.Replace("\r\n", "\n").Replace("\n", "\r\n");
            WriteFile(output, result);
            return rc;
        }
        finally
        {
            try
            {
                Directory.Delete(dir, true);
            }
            catch (Exception)
            {
            }
        }
    }

    private static int CountOf(string s, string sub)
    {
        int count = 0, at = 0;
        while ((at = s.IndexOf(sub, at, StringComparison.Ordinal)) >= 0)
        {
            count++;
            at += sub.Length;
        }
        return count;
    }

    // Test harness: reserialize or unwrap every file in a list into
    // outdir/<index>, so the corpus verifier can byte-compare this port
    // against the Python reference in one process. Files that fail to decode
    // produce <index>.error, matching the reference harness.
    private static int Batch(string mode, string listFile, string outDir)
    {
        Directory.CreateDirectory(outDir);
        string[] paths = File.ReadAllLines(listFile);
        for (int t = 0; t < paths.Length; t++)
        {
            if (paths[t].Length == 0)
                continue;
            try
            {
                string text = ReadFile(paths[t]);
                string result = mode == "--batch-unwrap"
                    ? Reserialize(text, Inf, Inf, false)
                    : Reserialize(text);
                WriteFile(Path.Combine(outDir, t.ToString()), result);
            }
            catch (Exception)
            {
                WriteFile(Path.Combine(outDir, t + ".error"), "");
            }
        }
        return 0;
    }

    private static int Main(string[] args)
    {
        try
        {
            if (args.Length == 3 && (args[0] == "--batch-reserialize" || args[0] == "--batch-unwrap"))
                return Batch(args[0], args[1], args[2]);
            if (args.Length < 4)
            {
                Console.Error.WriteLine("usage: uymf.exe BASE REMOTE LOCAL OUTPUT");
                return 1;
            }
            return Merge(args[0], args[1], args[2], args[3]);
        }
        catch (Exception e)
        {
            Console.Error.WriteLine("UnityYAMLMerge-Fix: " + e);
            return 1;
        }
    }
}
