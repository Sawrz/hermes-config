# Atomic replacement of writable working trees

Use this when reviewing a reconciler that builds a temporary directory and atomically swaps it with a live, writable checkout.

## Data-loss race

Validation immediately before `renameat2(RENAME_EXCHANGE)` does not close the race with ordinary writers:

1. Reconciler validates live checkout as clean and records its inode/HEAD.
2. A consumer creates or modifies a file in that checkout.
3. Reconciler exchanges the temporary clone with the live checkout.
4. The former live checkout now occupies the temporary path.
5. Unconditional cleanup recursively deletes it, including the consumer's new data.

A process-scoped sync lock does not protect against consumers that do not participate in that lock. This matters even when the supported boundary is a managed, replaceable checkout: replaceable remote-derived state does not authorize deletion of writes that arrived after the clean-state proof.

## Focused probe

Instrument the exact exchange boundary rather than merely testing dirty state before the run:

```python
real_exchange = module.atomic_exchange

def write_then_exchange(left, right):
    (right / "concurrent-data").write_text("must survive\n")
    real_exchange(left, right)

with mock.patch.object(module, "atomic_exchange", side_effect=write_then_exchange):
    run_sync()

assert (destination / "concurrent-data").exists()
```

If the run succeeds and the file disappears, the implementation has a confirmed data-loss race.

## Review requirements

- State the writer-coordination contract explicitly. A writable tree needs either a lock honored by every writer, filesystem-level exclusion, or a publication protocol that can detect and preserve post-validation writes.
- Re-checking inode, HEAD, and cleanliness before exchange is insufficient; there is always a gap before the namespace mutation.
- If exchange is used, inspect the displaced old tree before deleting it. If unexpected writes exist, preserve it and report an unambiguous recovery path; rollback itself must be race-safe.
- Validate races where the destination is replaced, renamed, or populated between the last check and the rename operation.
- Test interruption before exchange, immediately after exchange, during displaced-tree cleanup, and after success reporting.
- Fsync the publication parent before publishing durable success evidence. Atomic namespace mutation is not durable publication by itself.
- Reconcile bounded stale temporary directories after restart; language-level `finally` cleanup does not run after SIGKILL or machine failure.

## Observability checks

Failure metrics should cover configuration, destination validation, lock acquisition, network/authentication, publication, and cleanup failures. Preserve the previous last-success timestamp when recording a failure, and expose a bounded failure class. Ensure malformed parser input cannot escape the structured diagnostic boundary through generic exceptions such as URL-port `ValueError`.
