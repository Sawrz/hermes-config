# Containerized secret handoff and API-fixture review

Use these probes when a NixOS/systemd module renders credentials for a non-root container and when a safety CLI is tested against a fake HTTP API.

## Runtime credential readability

A bind mount preserves host ownership and mode. Before approving a generated `*_FILE` credential:

1. Determine the image's effective `USER` from the exact pinned image source/manifest.
2. Trace the host directory owner/mode, file owner/mode, and every parent directory.
3. Check that the container UID can traverse directories and read the mounted file while unrelated identities cannot.
4. Exercise the actual container startup path. An evaluation-only assertion that the volume and environment variable exist does not prove readability.

A root-owned `0750` runtime directory containing a root-owned `0600` file is unreadable to an image running as UID 65534, even when mounted read-only.

Also trace credential retirement, not only creation. A preserved runtime directory
plus conditional file creation can leave bootstrap or OIDC secrets readable by the
application UID after the feature is disabled. On every render, explicitly remove
obsolete conditional credentials before publication, or use separate lifecycle-
scoped credential mounts. Test enable → disable transitions and verify both the
host files and the container-visible paths are absent.

## Host shell versus container shell

For helpers that generate commands like `docker exec ... $(cat /run/secrets/x)`:

- Identify which shell expands command substitutions.
- Unquoted or shell-visible `$(...)` is normally evaluated by the host before `docker exec` starts.
- A path mounted only inside the container therefore fails on the host.
- Prefer passing a host-readable value safely as an argument, invoking an explicit shell inside the container with careful quoting, or checking the container's native health state.
- Add a runtime test that executes the generated pre-start command; string inspection is insufficient.

## Internal exposure is an end-to-end property

Edge metadata such as `accessPolicy = "internal-only"` does not constrain a Docker publication like `8080:8080`, which normally binds all host interfaces. Review together:

- host bind address;
- firewall policy;
- container/frontend network attachment;
- reverse-proxy route and access policy;
- metrics path exposure.

Tests should assert the effective port publication, not only declarative edge metadata.

## Real API shape versus permissive fakes

A fake server must preserve the real response schema. A common false green is applying request fields directly to a response object (`value.update(payload)`) even when the real API transforms them. For example, a mutation may accept `category_id` but return `category: {id: ...}` rather than a top-level `category_id`.

For every supported mutation:

1. Capture or model the exact pinned API's request and response shape.
2. Separate action payload fields from resulting-state assertions.
3. Test normal success and committed-response-lost reconciliation.
4. Verify nested ownership fields, not only the top-level resource owner.
5. Include malformed/foreign nested resources and duplicate reconciliation matches.

Beware fakes that apply `response.update(request_payload)`: they can invent top-level
fields that production returns only in nested form. For example, an API may accept
`category_id` but return `category: {id, user_id, ...}`. Verify the resulting nested
identity and ownership, and make the fake remove action-only request fields before
serializing its response.

Idempotent create helpers must enforce the full safe postcondition on every branch.
An exact identity match is insufficient when creation promises a disabled or
quarantined object: pre-existing matches and committed-response-lost reconciliation
must verify the safe state and requested parent/category, rather than returning an
enabled or differently attached object as success. Add adversarial tests for a
pre-existing unsafe match and a response-loss match with the wrong postcondition.

## Reserved environment completeness

When a module appends an escape hatch such as `extraEnvironment`, do not validate a
small hand-written denylist by inspection alone:

1. Extract the configuration-key namespace from the exact pinned upstream release.
2. Classify keys that affect credentials, authentication/user creation, listener
   behavior, lifecycle services, outbound fetch/proxy routing, and private-network
   access.
3. Compare that class mechanically against the module's reserved names/prefixes.
4. Include language/runtime proxy variables (`HTTP_PROXY`, `HTTPS_PROXY`,
   `NO_PROXY`, and case variants) when the application inherits their behavior
   outside its own option registry.
5. Add negative evaluation tests from several distinct classes; one rejected log
   or migration override does not prove completeness.

Prefer an explicit allowlist of harmless extension keys when the security contract
is narrow. A prefix denylist is only as complete as the upstream namespace it was
checked against.

## Mutation confirmation and readback consistency

Review confirmation policy across every path that can introduce the protected
state, not only dedicated update commands. If crawler/scraper/proxy policy requires
confirmation, a create command must not preload that policy without equivalent
confirmation merely because the new resource starts disabled. Ensure later enable
confirmation is bound to enough identity and policy context to make the operator's
decision meaningful.

For every mutation verb, build a table of: preview/read-before-write, exact
confirmation, dispatch, ambiguous-response reconciliation, and postcondition
readback. Check normal and response-lost branches separately. A refresh/action may
not have an immediate observable completion state, but it should still perform the
strongest available identity/state readback and must not return a stale pre-write
object as if it were post-write evidence.

## Restore-test completeness

A real dump/drop/restore is necessary but not sufficient. After restore, query every promised authority class directly (accounts, categories, subscriptions, entries/read state), and repeat cross-account denial probes. If the production artifact is a container stack, include at least one test of that stack's credential mounts, startup ordering, and backup adapter; a native package test cannot establish those properties.
