# Mealie OpenAPI regression fixture

Extracted from https://demo.mealie.io/openapi.json on 2026-10-14
(OpenAPI 3.1.0, application version `nightly`). This is public API metadata,
not household data or a claim about the installed deployment's version.

The three request bodies (recipe create/update and meal-plan create) and all
34 transitively referenced component schemas are unchanged. Unrelated paths,
responses and descriptive application metadata are omitted; empty GET operations
support the local HTTP fixture's reconciliation and readback. Tests run offline.

Original full document SHA-256: `892def200187b41e185e243db865fd59a41ebe4c5198c474613ae34f06984d4e`.
