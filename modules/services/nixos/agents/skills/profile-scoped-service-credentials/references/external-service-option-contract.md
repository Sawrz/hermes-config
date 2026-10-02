# External-service option contract

Use a typed external-service declaration to bind an existing service to one profile without embedding service policy in the profile or skill.

## Shape

A binding may provide:

- `endpoint`: canonical service root;
- `credentialFiles`: named secret runtime files;
- `textFiles`: named non-secret runtime files;
- `capabilities`: supported service interfaces selected by the profile;
- `scope`: explicit include/exclude limits understood by the service adapter.

The service capability owns validation of supported fields and operations. The agent contract decides whether the profile receives the binding and what authority it has.

## Validation

Reject:

- duplicate runtime filenames across credential and text fields;
- empty, relative, or unsupported endpoints;
- unknown capability names;
- a selected service capability without the files it requires;
- multiple bindings that collide on the same runtime destination;
- implicit inheritance by a dispatcher, default profile, sibling profile, or unrelated service.

Credential sources and non-secret sources must remain distinct. Do not infer a secret-manager path, credential identity, host, user, schedule, service owner, or deployment target from the service name.

## Materialization

Expose only the files required by the selected service capability. Mount or stage credential files read-only where supported, use stable runtime filenames, and make unrelated bindings absent rather than unreadable by convention.

Shared implementation helpers are acceptable, but each profile's binding, source references, runtime tree, and generated configuration remain independently validated. A shared process identity or container runtime is not proof of credential isolation; state the actual trust boundary honestly.

## Verification

- Evaluate the profile with only the declared service binding.
- Confirm required runtime files are present and collision-free.
- Confirm unrelated profiles and services receive no mount or generated path.
- Inspect generated environment and arguments for credential leakage.
- Exercise a harmless authenticated operation to prove effective identity and scope.
- Distinguish authentication, authorization, connectivity, and schema failure.

Service provisioning, secret generation, profile authority, scheduling, migration, rotation, revocation, and removal are outside this option contract.
