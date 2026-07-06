//! P12: the codec must never panic on arbitrary bytes. Idempotence is NOT
//! asserted here: on non-editor-form input the reference itself is not
//! idempotent, pinned by codec::tests::garbage_nested_quote_matches_reference.
//! Idempotence on editor-form input is proven by the corpus differential.
#![no_main]
use libfuzzer_sys::fuzz_target;

const INF: usize = 1_000_000_000;

fuzz_target!(|data: &[u8]| {
    if let Ok(s) = std::str::from_utf8(data) {
        let r = uymerge::codec::reserialize(s, 79, 80, true);
        let _ = uymerge::codec::reserialize(&r, 79, 80, true);
        let u = uymerge::codec::reserialize(s, INF, INF, false);
        let _ = uymerge::codec::reserialize(&u, 79, 80, true);
    }
});
