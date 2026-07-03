.PHONY: check fmt clippy test pytest build

check: fmt clippy test pytest

fmt:
	cargo fmt --check

clippy:
	cargo clippy --all-targets -- -D warnings

test:
	cargo test

pytest:
	python3 -m pytest tests/ -q

build:
	cargo build --release
