# Security policy

## Reporting

Do not open a public issue containing credentials, prompts, model output, ERP data or exploitable details. Report privately to the repository owner with the affected commit, impact, reproduction steps and suggested containment. Rotate any credential that has appeared in chat, logs, screenshots or Git history before treating the incident as closed.

## Supported versions

Security fixes target the current `main` release and the active `develop` integration branch. Deployments must pin an immutable image digest or repository commit; mutable `latest` tags are not a supported audit boundary.

## Security invariants

- Browsers and mobile clients never receive the internal service token and never call the Orchestrator directly.
- The Orchestrator does not connect to the ERP database and does not hold an ERP administrator account.
- Frappe supplies authenticated, permission-filtered and minimized business context.
- LiteLLM, Langfuse and service credentials are injected at runtime and are never committed.
- Raw content capture is disabled by default; observability failure remains fail-open for the business response.
- Formal ERP writes remain in Frappe/ERPNext. AI draft endpoints only return candidates.

See `docs/SECURITY.zh-CN.md` for the detailed threat model and operational controls.
