## Summary

- What changed and why?

## Risk and contracts

- [ ] Authentication, permission and service-token boundaries were reviewed.
- [ ] Prompt/model/Embedding version compatibility was reviewed.
- [ ] Failure, retry, fallback and rollback behavior was reviewed.
- [ ] No secret, raw production prompt, model output or ERP data is included.

## Verification

- [ ] `uv run ruff check .`
- [ ] `docker build --target test -t myapp-ai:test . && docker run --rm myapp-ai:test`
- [ ] Standalone Compose integration test, when runtime/dependency behavior changed
- [ ] Documentation and configuration examples updated
