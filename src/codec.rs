//! Editor-faithful unwrap/rewrap of raw Unity YAML text.
//! SPEC section 2. Packets P1 (terminators, plain scalars), P2 (quoted
//! scalars, flow cleanup), P3 (reserialize dispatch, byte parity).
//! Reference functions: split_lines, reemit_plain, join_plain_value,
//! gather_continuations, gather_quoted, reemit_quoted, decode_quoted,
//! reemit_double, decode_double, reserialize.
