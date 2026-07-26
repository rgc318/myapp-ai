# Changelog

All notable changes are recorded here. The project follows semantic versioning once release tags are created.

## Unreleased

- Upgraded structured intent parsing to `erp-intent-v3`: state-aware natural-language continuation, current-message precedence, and ambiguity-safe entity inheritance while retaining strict extra-field rejection and filter validation.
- Added bounded `conversation-state-v1` context handling for multi-turn intent parsing; state is treated as a parsing aid, never as live ERP truth.
- Added offline natural-language evaluation coverage for mixed purchase documents and custom cashflow ranges.
- Added strict JSON Schema query-intent parsing for controlled Frappe routing and natural-language evaluation cases.
- Added standalone Compose deployment with Redis and Qdrant.
- Added deterministic dependency locking, source quality, dependency audit, image scanning and CodeQL.
- Added standalone Chat/vector integration tests and repository governance files.
- Added an independent enterprise documentation set.

## 0.1.0 - 2026-07-16

- Extracted the AI Orchestrator into an independent repository while preserving its history.
- Established Docker test/runtime CI and GHCR release publishing with provenance and SBOM.
- Delivered governed Chat/SSE, structured drafts, model policy validation, Qdrant semantic retrieval, Langfuse delivery and fixed evaluation capabilities.
