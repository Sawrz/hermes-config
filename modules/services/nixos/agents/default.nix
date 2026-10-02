{ ... }:

{
  imports = [
    ./forgejo-event-ingest.nix
    ./forgejo-kanban-workflow.nix
    ./hermes.nix
    ./hermes-paperclip.nix
    ./kanban-vikunja-projection.nix
    ./capabilities/cooking-apis.nix
    ./capabilities/container-image-maintenance.nix
    ./capabilities/digest.nix
    ./capabilities/foundation-skills.nix
    ./capabilities/jobs.nix
    ./capabilities/large-task-orchestration.nix
    ./capabilities/miniflux-source.nix
    ./capabilities/repository-docs-audit.nix
    ./capabilities/nix-maintenance-runtime.nix
    ./capabilities/wger-api.nix
    ./capabilities/vikunja-task-reviews.nix
    ./capabilities/nutrition-workflows.nix
    ./capabilities/repository-sync.nix
  ];
}
