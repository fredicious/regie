.PHONY: test web build dev

test:
	cargo test
	@if [ -f web/package.json ]; then pnpm -C web test && pnpm -C web typecheck; fi

web:
	pnpm -C web install && pnpm -C web build

build: web
	cargo build --release

dev:
	cargo run -p regie
