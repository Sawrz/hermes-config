# Vikunja review helper contract

## Supported operations

The helper provides:

- API-v2 saved-filter readback;
- completely paginated task collection with explicit caps;
- configured section constraints and deterministic deduplication;
- deterministic Markdown rendering with untrusted-title escaping;
- recurrence classification without a false quarterly claim;
- narrow task-mutation preview, exact source-revision confirmation, one write, and exact readback.

Configuration supplied by the caller owns concrete project and saved-filter IDs. The helper validates those values but does not choose them.

## API boundary

The supported API-v2 surface is:

- `GET /filters/{filter_id}` for saved-filter readback;
- `GET /tasks` with complete pagination;
- `GET` and `PATCH /tasks/{projecttask}` for interactive read/update;
- task recurrence fields `repeat_after: integer` and `repeat_mode: 0|1|2`.

The API does not advertise `If-Match` for task PATCH. The helper binds confirmation to a fresh source revision and verifies the result after one write, but cannot eliminate the race between its final pre-write read and PATCH. A transport failure during the PATCH is indeterminate until an independent readback resolves it.

Exact quarterly calendar recurrence is not represented. A 90-day duration remains approximate and must be labeled as such.

## Outside this contract

This helper does not own profile selection, project ownership, filter policy, schedules, credential materialization, persistent-path provisioning, report delivery, or delivery acknowledgement. Those belong to the caller's profile and integration configuration.
