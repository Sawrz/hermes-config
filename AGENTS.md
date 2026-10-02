# Project workflow

Edit this repository's reusable implementation and tests. Deployment-specific
choices belong to nix-config. Do not modify live configuration or private runtime
knowledge to apply changes early. Use Forgejo for development and writes; GitHub
is a recovery mirror. Keep existing runtime state and identifiers intact. Build,
activation and runtime validation are separate outcomes.
