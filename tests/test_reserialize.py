"""Codec self-consistency on the scalar shapes that break line-based tools.
These are the quirks surfaced while reverse-engineering the editor's
serializer: significant whitespace before folds, quote and backslash
escapes, blank lines inside quoted scalars, astral-width counting.

The editor-faithfulness oracle is byte-equality over a real asset corpus,
see README. These tests pin the invariants the driver relies on: rewrap is
idempotent, and unwrapping never loses information."""
import pytest

from conftest import HDR, uymf

INF = uymf.INF


def rewrap(text):
    return uymf.reserialize(text)


def unwrap(text):
    return uymf.reserialize(text, INF, INF, fix_empty=False)


SCALARS = [
    pytest.param("  m_Localized: '%s'" % ("word " * 30 + "end"), id="quoted-folds"),
    pytest.param("  m_Localized: 'alpha  beta%s  gamma'" % (" tail" * 20), id="double-space"),
    pytest.param("  m_Localized: 'it''s %s quote'" % ("very " * 25), id="escaped-quote"),
    pytest.param("  m_Localized: 'para one\n\n    para two %s'" % ("x " * 30), id="blank-line"),
    pytest.param('  m_Localized: "line\\r\\nnext %s\\u2019s"' % ("w " * 30), id="dq-escapes"),
    pytest.param('  m_Localized: "spaced\\ %s end"' % ("f " * 35), id="dq-escaped-space"),
    pytest.param("  m_Localized: %s plain scalar over the width" % ("p " * 35), id="plain-folds"),
    pytest.param("  - Some.Assembly.Type, Assembly-CSharp, Version=1.0.0.0, Culture=neutral,"
                 " PublicKeyToken=null and some more", id="bare-seq-scalar"),
    pytest.param("  m_Localized: '\U0001f600 emoji %s wide'" % ("e " * 30), id="astral-width"),
    pytest.param("  - m_Key: nested %s deep" % ("n " * 35), id="dash-key"),
]


class TestCodecInvariants:

    @pytest.mark.parametrize("body", SCALARS)
    def test_rewrap_idempotent(self, body):
        doc = HDR + body + "\n"
        once = rewrap(doc)
        assert rewrap(once) == once

    @pytest.mark.parametrize("body", SCALARS)
    def test_unwrap_is_lossless(self, body):
        # the driver feeds unwrap output to the native tool and rewraps its
        # result. A no-op merge must reproduce the canonical form exactly.
        doc = HDR + body + "\n"
        assert rewrap(unwrap(rewrap(doc))) == rewrap(doc)

    @pytest.mark.parametrize("body", SCALARS)
    def test_unwrap_canonicalizes(self, body):
        # both merge sides are unwrapped before the native tool sees them.
        # Folded and unfolded forms of one value must unwrap identically.
        doc = HDR + body + "\n"
        assert unwrap(doc) == unwrap(rewrap(doc))

    def test_folded_output_respects_width(self):
        doc = HDR + "  m_Localized: '%s'\n" % ("word " * 40 + "end")
        out = rewrap(doc)
        assert len(out.split("\n")) > len(doc.split("\n"))     # it folded
        for line in out.split("\n"):
            # a line ends at the first break past the width, so it may
            # overrun by at most one word
            assert len(line) <= uymf.QUOTED_WIDTH + len("word") + 1


class TestTerminators:

    def test_crlf_document(self):
        doc = (HDR + "  m_Localized: '%s'\n" % ("word " * 30)).replace("\n", "\r\n")
        once = rewrap(doc)
        assert rewrap(once) == once
        assert "\r\n" in once and "\n\n" not in once.replace("\r\n", "")

    def test_mixed_terminator_quoted_block_passes_verbatim(self):
        # a quoted block whose lines disagree on CR is left untouched, since
        # a terminator is never invented
        block = "  m_Localized: 'first part\r\n    second part'\n"
        doc = HDR + block
        assert block in rewrap(doc)


class TestEmptyFlow:

    def test_injected_empty_string_stripped_only_in_flow(self):
        doc = HDR + "  type: {class: '', ns: '', asm: X}\n  plain: ''\n"
        out = rewrap(doc)
        assert "{class: , ns: , asm: X}" in out
        assert "plain: ''" in out                      # a real empty value is untouched

    def test_unwrap_keeps_empty_flow_verbatim(self):
        doc = HDR + "  type: {class: '', ns: }\n"
        assert "{class: '', ns: }" in unwrap(doc)
