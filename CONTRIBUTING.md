# Contributing

Development targets Python 3.12 and Docker BuildKit. Create feature and fix branches from `develop`; promote verified releases from `develop` to `main`.

```bash
uv sync --extra test --extra dev --frozen
uv run ruff check .
docker build --target test -t myapp-ai:test .
docker run --rm myapp-ai:test
```

When runtime integration changes, copy `.env.example` to `.env`, use synthetic credentials, and run `make integration`. Never use production ERP data or billable providers in default CI.

Changes must preserve service-token authentication, Frappe permission boundaries, prompt/version conflicts, bounded concurrency, fail-closed governance limits and fail-open observability. Update the relevant document under `docs/` whenever a contract, variable, operational procedure or release gate changes.
