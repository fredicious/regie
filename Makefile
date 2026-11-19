.PHONY: test web build dev

test:
	cargo test
	pnpm -C web test
	pnpm -C web typecheck

web:
	pnpm -C web install --frozen-lockfile && pnpm -C web build

build: web
	cargo build --release

dev:
	@echo "Run these in two terminals:"
	@echo "  cargo run -p regie -- serve --no-open"
	@echo "  pnpm -C web dev"
