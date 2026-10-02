# hermes-vikunja-review

Deterministic Vikunja API-v2 daily/weekly review collector for native Hermes `no_agent` cron jobs.

## Contract

- Scheduled runs are GET-only. The process prints one rendered review only when content changed; empty and unchanged runs print zero bytes.
- Durable state uses the shared `hermes-workflow-state` generation protocol. Failed or partial collections do not advance the selected generation.
- Review state records `last_rendered` plus the rendered content digest. `rendered` means output generation and durable local persistence succeeded; it does not prove that native Hermes handed the output to the destination or that the destination accepted it.
- Every configured saved filter is read back before use. Pagination must be complete and remain within declared page/task caps.
- Weekly review is the sole Inbox owner. A daily result containing an Inbox-project task fails closed.
- Dynamic preferences can disable the workflow or narrow configured sections. They cannot add filter IDs, project IDs, API roots, task fields, or execution modes.
- Native Hermes cron remains the only delivery owner. The collector has no Telegram client and no model/agent path.
- The accepted delivery policy is best-effort and duplicate-averse: state advances before stdout is handed to native Hermes, so a failure after persistence can suppress that occurrence from automatic retry. Recovery requires operator reconciliation and a manual resend may duplicate an ambiguously delivered review.

## Deployed API-v2 facts verified on 2026-08-28

The deployed Vikunja 2.5.0 OpenAPI and harmless read-only probes showed:

- endpoint paths `/filters/{filter}` and `/tasks`;
- paginated task envelopes include `$schema`, `items`, `page`, `per_page`, `total`, and `total_pages`;
- empty task collections use `total_pages = 0`;
- `sort_by` and `order_by` are repeatable query fields; saved-filter `s` maps to API-v2 `q`;
- recurrence is `repeat_after: integer` plus `repeat_mode: 0|1|2`;
- task PATCH does not advertise an `If-Match` parameter, despite generic API-v2 documentation describing write preconditions.

The mutation helper therefore binds its confirmation to task ID, source `updated` revision, and exact patch; performs a fresh pre-write read; sends exactly one PATCH; and performs exact readback. This narrows but cannot remove the race between the final read and PATCH. An ambiguous write is never retried automatically.

## Package outputs

- `bin/hermes-vikunja-review`
- `share/hermes-vikunja-review/review-preferences.json`
- `share/hermes-vikunja-review/review-config.example.json`

The example config contains placeholders only. P70 profile integration owns actual project/filter IDs, rendered config paths, native cron jobs, and profile selections.
