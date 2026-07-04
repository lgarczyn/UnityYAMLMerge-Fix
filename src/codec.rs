//! Editor-faithful unwrap/rewrap of raw Unity YAML text.
//! SPEC section 2. Packets P1 (terminators, plain scalars), P2 (quoted
//! scalars, flow cleanup), P3 (reserialize dispatch, byte parity).
//! Reference functions: split_lines, reemit_plain, join_plain_value,
//! gather_continuations, gather_quoted, reemit_quoted, decode_quoted,
//! reemit_double, decode_double, reserialize.
//!
//! Everything works on raw text lines, never decoding to a YAML value:
//! Unity's pre-fold whitespace is significant, so the oracle is byte
//! equality with the editor's own serializer. Columns count Unicode code
//! points, i.e. `char`s, not UTF-16 units and not bytes.

/// The "unwrap" width: wide enough that a scalar never folds.
/// Matches the reference `INF = 10 ** 9`.
pub const INF: usize = 1_000_000_000;

/// Reference `KEY = ^(\s*(?:- )*)([\w.\-/]+):\s(.+)$` as a matcher.
/// The leading group absorbs sequence dashes so `- k: v` and nested
/// `- - k: v` still fold; the value keeps its trailing spaces. Returns
/// (indent, key, value) on a match.
pub fn key_match(line: &str) -> Option<(String, String, String)> {
    let ch: Vec<char> = line.chars().collect();
    let n = ch.len();

    // \s* leading whitespace.
    let mut p = 0;
    while p < n && ch[p].is_whitespace() {
        p += 1;
    }

    // (?:- )* greedy. Record the end position after each "- " copy.
    let mut dash_ends = Vec::new();
    let mut q = p;
    while q + 1 < n && ch[q] == '-' && ch[q + 1] == ' ' {
        q += 2;
        dash_ends.push(q);
    }

    // The regex tries the most "- " copies first, then backtracks toward
    // zero. \s* can never give ground: a space fits neither the dash run
    // nor a key char, so only the "- " count varies.
    let candidates = dash_ends.iter().rev().copied().chain(std::iter::once(p));
    for g1_end in candidates {
        // [\w.\-/]+ greedy. The char after the run must be ':', so a
        // shorter key never helps: nothing shorter ends on a ':'.
        let mut k = g1_end;
        while k < n && is_key_char(ch[k]) {
            k += 1;
        }
        if k == g1_end || k >= n || ch[k] != ':' {
            continue;
        }
        let colon = k;
        // :\s one whitespace char, then (.+)$ at least one more char.
        if colon + 2 >= n || !ch[colon + 1].is_whitespace() {
            continue;
        }
        let indent: String = ch[..g1_end].iter().collect();
        let key: String = ch[g1_end..colon].iter().collect();
        let value: String = ch[colon + 2..].iter().collect();
        return Some((indent, key, value));
    }
    None
}

// A KEY key character: `\w` (Unicode word), plus `.`, `-`, `/`. Unity keys
// are ASCII identifiers, so is_alphanumeric stands in for Python's \w.
fn is_key_char(c: char) -> bool {
    c.is_alphanumeric() || c == '_' || c == '.' || c == '-' || c == '/'
}

/// Reference `split_lines`: split on `\n`, recording a trailing `\r` per
/// line so mixed LF/CRLF assets round-trip. Returns (content, had_cr).
pub fn split_lines(text: &str) -> Vec<(String, bool)> {
    text.split('\n')
        .map(|p| match p.strip_suffix('\r') {
            Some(rest) => (rest.to_string(), true),
            None => (p.to_string(), false),
        })
        .collect()
}

/// Reference `reemit_plain`: fold a plain scalar at the last space of a run
/// once the column passes `width`. A fold never splits inside a space run;
/// earlier spaces stay as trailing whitespace. The first line carries
/// `prefix`, later lines carry `cont_indent`.
pub fn reemit_plain(value: &str, prefix: &str, cont_indent: &str, width: usize) -> Vec<String> {
    let v: Vec<char> = value.chars().collect();
    let n = v.len();
    let mut out = Vec::new();
    let mut cur = String::from(prefix);
    let mut col = prefix.chars().count();
    for i in 0..n {
        let c = v[i];
        let next_is_space = i + 1 < n && v[i + 1] == ' ';
        if c == ' ' && col > width && !next_is_space {
            out.push(std::mem::replace(&mut cur, cont_indent.to_string()));
            col = cont_indent.chars().count();
        } else {
            cur.push(c);
            col += 1;
        }
    }
    out.push(cur);
    out
}

/// Reference `join_plain_value`: inverse of `reemit_plain`. Each
/// continuation contributes one restored fold space plus its content past
/// `cont_indent`.
pub fn join_plain_value(val: &str, conts: &[String], cont_indent: &str) -> String {
    let ci_len = cont_indent.chars().count();
    let mut s = String::from(val);
    for c in conts {
        s.push(' ');
        s.extend(c.chars().skip(ci_len));
    }
    s
}

/// Reference `gather_continuations`: from line `i`, take strictly
/// more-indented non-blank lines that are not themselves KEY mappings.
/// Returns the continuation lines and the index just past them.
pub fn gather_continuations(lines: &[String], i: usize, key_indent: usize) -> (Vec<String>, usize) {
    let mut conts = Vec::new();
    let mut j = i + 1;
    while j < lines.len() {
        let c = &lines[j];
        let ci = c.chars().take_while(|&ch| ch == ' ').count();
        if c.trim().is_empty() || ci <= key_indent || key_match(c).is_some() {
            break;
        }
        conts.push(c.clone());
        j += 1;
    }
    (conts, j)
}

#[cfg(test)]
mod tests {
    use super::*;
    use proptest::prelude::*;

    #[test]
    fn split_lines_records_cr_per_line() {
        let got = split_lines("a\r\nb\nc\r");
        assert_eq!(
            got,
            vec![
                ("a".to_string(), true),
                ("b".to_string(), false),
                ("c".to_string(), true),
            ]
        );
    }

    #[test]
    fn split_lines_trailing_newline_yields_empty_tail() {
        assert_eq!(
            split_lines("x\n"),
            vec![("x".to_string(), false), (String::new(), false)]
        );
    }

    #[test]
    fn key_match_plain() {
        let (i, k, v) = key_match("  m_Name: Table_en").unwrap();
        assert_eq!(
            (i.as_str(), k.as_str(), v.as_str()),
            ("  ", "m_Name", "Table_en")
        );
    }

    #[test]
    fn key_match_absorbs_sequence_dashes() {
        let (i, k, v) = key_match("  - - m_Key: value here").unwrap();
        assert_eq!(
            (i.as_str(), k.as_str(), v.as_str()),
            ("  - - ", "m_Key", "value here")
        );
    }

    #[test]
    fn key_match_keeps_trailing_spaces_in_value() {
        let (_, _, v) = key_match("  k:  x  ").unwrap();
        // one \s eats the first space; the rest is group 3 verbatim.
        assert_eq!(v, " x  ");
    }

    #[test]
    fn key_match_rejects_bare_sequence_and_empty_value() {
        assert!(key_match("  - Some.Assembly.Type, Version=1").is_none());
        assert!(key_match("  key: ").is_none()); // \s eats the space, nothing left
        assert!(key_match("  key:").is_none());
    }

    #[test]
    fn reemit_plain_infinite_width_never_folds() {
        let out = reemit_plain("a b c d e", "  k: ", "    ", INF);
        assert_eq!(out, vec!["  k: a b c d e".to_string()]);
    }

    #[test]
    fn reemit_plain_folds_at_last_space_of_a_run() {
        // width 3: fold triggers once col > 3 at a run's last space; the
        // earlier spaces stay as trailing whitespace.
        let out = reemit_plain("aa    bb", "", "", 3);
        assert_eq!(out, vec!["aa   ".to_string(), "bb".to_string()]);
    }

    #[test]
    fn gather_continuations_stops_at_key_blank_and_dedent() {
        let lines: Vec<String> = [
            "  k: v",         // 0, the key line
            "    more text",  // 1, continuation
            "    k2: nested", // 2, a KEY line, stops
        ]
        .iter()
        .map(|s| s.to_string())
        .collect();
        let (conts, j) = gather_continuations(&lines, 0, 2);
        assert_eq!(conts, vec!["    more text".to_string()]);
        assert_eq!(j, 2);
    }

    #[test]
    fn gather_continuations_stops_on_dedented_sibling() {
        let lines: Vec<String> = ["  k: v", "  sibling: x"]
            .iter()
            .map(|s| s.to_string())
            .collect();
        let (conts, j) = gather_continuations(&lines, 0, 2);
        assert!(conts.is_empty());
        assert_eq!(j, 1);
    }

    proptest! {
        // reemit_plain then join_plain_value round-trips any plain value:
        // folds drop exactly one space each, join restores exactly one.
        #[test]
        fn reemit_join_roundtrip(
            value in "[a-zA-Z ]{0,80}",
            prefix_len in 0usize..6,
            cont_len in 0usize..6,
            width in 1usize..40,
        ) {
            let prefix = " ".repeat(prefix_len);
            let cont_indent = " ".repeat(cont_len);
            let lines = reemit_plain(&value, &prefix, &cont_indent, width);
            let first = lines[0].strip_prefix(&prefix).unwrap();
            let joined = join_plain_value(first, &lines[1..], &cont_indent);
            prop_assert_eq!(joined, value);
        }

        // At INF width a value is a single line, byte-for-byte prefix+value.
        #[test]
        fn reemit_infinite_is_single_line(
            value in "[^\n]{0,120}",
            prefix_len in 0usize..6,
        ) {
            let prefix = " ".repeat(prefix_len);
            let lines = reemit_plain(&value, &prefix, "  ", INF);
            prop_assert_eq!(lines.len(), 1);
            prop_assert_eq!(&lines[0], &format!("{}{}", prefix, value));
        }

        // split_lines is exactly reversible by re-joining content+terminator.
        #[test]
        fn split_lines_reversible(text in "[a-zA-Z\r\n]{0,60}") {
            let rebuilt: String = split_lines(&text)
                .iter()
                .map(|(c, cr)| format!("{}{}", c, if *cr { "\r" } else { "" }))
                .collect::<Vec<_>>()
                .join("\n");
            prop_assert_eq!(rebuilt, text);
        }
    }
}
