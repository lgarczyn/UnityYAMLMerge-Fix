# unity_yaml_formatter.py
import re

YAML_BEST_WIDTH = 80
_CONTINUATION_INDENT = "      "  # 6 spaces

_YAML_FLOW_INDICATORS = set(':{}[]|>&*!,#`"\'@%')


def _encode_double_quoted(value: str) -> str:
    out = []
    for c in value:
        code = ord(c)
        if c == '\\':
            out.append('\\\\')
        elif c == '"':
            out.append('\\"')
        elif c == '\n':
            out.append('\\n')
        elif c == '\r':
            out.append('\\r')
        elif c == '\t':
            out.append('\\t')
        elif code > 0xFF:
            out.append(f"\\u{code:04X}")
        elif code > 0x7F:
            out.append(f"\\x{code:02X}")
        elif code < 0x20:
            out.append(f"\\x{code:02X}")
        else:
            out.append(c)
    return "".join(out)


def _needs_double_quoting(value: str) -> bool:
    if not value:
        return False
    if value[0] in _YAML_FLOW_INDICATORS:
        return True
    if value[0] in (' ', '\t') or value[-1] in (' ', '\t'):
        return True
    if '\n' in value or '\r' in value:
        return True
    for c in value:
        if ord(c) > 0x7F:
            return True
    if ': ' in value or value.endswith(':'):
        return True
    return False


def _wrap_double_quoted(escaped: str) -> str:
    opening_col = len('    m_Localized: "')  # 18, for column tracking only
    cont_indent = _CONTINUATION_INDENT
    width = YAML_BEST_WIDTH

    out = []
    cur = '"'
    vcol = opening_col
    first = True

    for word in escaped.split(" "):
        if first:
            cur += word
            vcol += len(word)
            first = False
        elif word != "" and vcol + 1 > width:
            out.append(cur)
            cur = cont_indent + word
            vcol = len(cont_indent) + len(word)
        else:
            cur += " " + word
            vcol += 1 + len(word)

    out.append(cur + '"')
    return "\n".join(out)


def format_localized_value(value: str) -> str:
    if not value:
        return value
    if not _needs_double_quoting(value):
        return value
    escaped = _encode_double_quoted(value)
    return _wrap_double_quoted(escaped)


def _decode_existing_value(raw: str) -> str:
    # Decode an existing m_Localized value back to a plain Python string.
    # Handles multi-line wrapped double-quoted scalars.
    # Derived from decode_double() in unityyamlmerge_fix.py.
    stripped = raw.strip()
    if not stripped.startswith('"'):
        return stripped

    # Strip opening quote; closing quote is on the last physical line
    # Join continuation lines with a single space (soft fold), strip indent
    lines = raw.split("\n")
    first_line = lines[0].strip()

    if len(lines) == 1:
        # single line: strip surrounding quotes
        inner = first_line[1:]
        if inner.endswith('"'):
            inner = inner[:-1]
    else:
        # multi-line: first line loses leading ", last line loses trailing "
        first = first_line[1:]
        rest = [l.strip() for l in lines[1:]]
        last = rest[-1]
        if last.endswith('"'):
            rest[-1] = last[:-1]
        inner = " ".join([first] + rest)

    # Unescape: process escape sequences
    out = []
    i = 0
    while i < len(inner):
        if inner[i] == '\\' and i + 1 < len(inner):
            nc = inner[i + 1]
            if nc == 'n':
                out.append('\n')
                i += 2
            elif nc == 'r':
                out.append('\r')
                i += 2
            elif nc == 't':
                out.append('\t')
                i += 2
            elif nc == '"':
                out.append('"')
                i += 2
            elif nc == '\\':
                out.append('\\')
                i += 2
            elif nc == 'u' and i + 5 < len(inner):
                out.append(chr(int(inner[i + 2 : i + 6], 16)))
                i += 6
            elif nc == 'x' and i + 3 < len(inner):
                out.append(chr(int(inner[i + 2 : i + 4], 16)))
                i += 4
            else:
                out.append(inner[i])
                i += 1
        else:
            out.append(inner[i])
            i += 1
    return "".join(out)


_ENTRY_RE = re.compile(
    r"(m_Id: (\d+)\n    m_Localized: )(.*?)(\n    m_Metadata:)",
    re.DOTALL,
)

_FALLBACK_TEMPLATE = (
    "  - m_Id: __ID__\n"
    "    m_Localized: __VALUE__\n"
    "    m_Metadata:\n"
    "      m_Items: []"
)


def detect_entry_template(content: str) -> str:
    blocks = re.findall(
        r"  - m_Id: \d+\n.*?(?=\n  - m_Id:|\n  references:|\Z)",
        content,
        re.DOTALL,
    )
    if not blocks:
        return _FALLBACK_TEMPLATE

    block = blocks[0]
    block = re.sub(r"(  - m_Id: )\d+", r"\g<1>__ID__", block)
    block = re.sub(
        r"(    m_Localized: ).*?(\n    m_Metadata:)",
        r"\g<1>__VALUE__\g<2>",
        block,
        flags = re.DOTALL,
    )
    return block


def fill_missing_values(content: str, translations: dict[str, str]) -> tuple[str, list[str]]:
    filled: list[str] = []

    def replace_entry(m: re.Match) -> str:
        prefix = m.group(1)
        entry_id = m.group(2)
        raw = m.group(3)
        suffix = m.group(4)

        current = _decode_existing_value(raw)
        if current.strip():
            return m.group(0)

        new_value = translations.get(entry_id, "")
        if not new_value or not new_value.strip():
            return m.group(0)

        filled.append(entry_id)
        return f"{prefix}{format_localized_value(new_value)}{suffix}"

    updated = _ENTRY_RE.sub(replace_entry, content)
    return updated, filled


def append_new_entries(content: str, translations: dict[str, str], template: str | None = None) -> tuple[str, list[str]]:
    if template is None:
        template = detect_entry_template(content)

    existing_ids = set(re.findall(r"m_Id: (\d+)", content))
    new_block = ""
    appended: list[str] = []

    for entry_id, value in translations.items():
        if entry_id in existing_ids:
            continue
        if not value or not value.strip():
            continue

        formatted = format_localized_value(value)
        entry_block = template.replace("__ID__", entry_id).replace("__VALUE__", formatted)
        new_block += entry_block + "\n"
        appended.append(entry_id)

    if new_block:
        content = re.sub(
            r"\n(  references:)",
            lambda m: "\n" + new_block + m.group(1),
            content,
        )

    return content, appended
