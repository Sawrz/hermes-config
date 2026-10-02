# Nested runtime-directory ownership

Use this note when credential materialization needs a nested runtime path whose parent directories may not exist on first use.

## Failure mode

Creating only the final credential directory is insufficient when an intermediate parent is missing or owned by the wrong identity. Retained runtime directories can hide the problem until a new binding is activated.

Diagnose the complete path, not only the leaf:

```bash
namei -om /run/hermes-credentials/services/<service>
stat -Lc '%A %a %U:%G %n' <each-path-component>
```

Also verify the effective service identity and whether the process actually restarted. Do not assume a sandbox defect from `Permission denied`; prove the failing path component before changing isolation settings.

## Durable declaration

Declare every ownership boundary needed for first use:

```text
runtime root:             traversable by the service identity
service parent:           owned by the materialization owner
credential-bearing leaf:  private to the intended consumer
```

Derive paths from the typed service binding, not from hardcoded profile names. Create directories only for selected services and keep credential-bearing leaves restrictive. Materialize files atomically after the parent chain is valid.

## Regression coverage

Test both an established binding and a fresh binding from an empty runtime tree. Assert:

- required parent and leaf declarations exist;
- ownership and modes permit the intended materializer and consumer only;
- unrelated services receive no directories;
- repeated materialization is idempotent;
- failure leaves no partial credential file.

## Runtime verification

After activation, verify the intended system closure, tmpfiles/materialization result, complete path ownership, successful pre-start checks, and a harmless authenticated operation. A manual repair or restart may aid diagnosis but is not the durable fix.
