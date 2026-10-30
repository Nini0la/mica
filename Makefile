backend-install:
	cd backend && uv sync --extra dev
backend-test:
	cd backend && uv run pytest -q
backend-check:
	cd backend && uv run ruff check . && uv run mypy app tests
frontend-install:
	cd frontend && npm ci
frontend-check:
	cd frontend && npm test && npm run audit:ci && npm run lint && npm run typecheck && npm run build
check: backend-test backend-check frontend-check
