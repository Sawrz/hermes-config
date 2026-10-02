# Repository authority is explicit and independent from profile identity.
{
  config,
  lib,
  pkgs,
  ...
}:
let
  cfg = config.custom.services.hermes;
  profiles = lib.mapAttrs (
    name: profile: lib.recursiveUpdate profile (config.services.hermes-agent.profiles.${name} or { })
  ) cfg.profiles;
  selected =
    p: name:
    let
      v = p.serviceIntegrations.${name} or false;
    in
    p.enable && (if builtins.isBool v then v else v.enable);
  settings =
    p: name:
    let
      v = p.serviceIntegrations.${name} or false;
    in
    if builtins.isBool v then { } else v.settings;
  authority = cfg.repositoryAuthority;
  declarations = authority.repositories;
  ownerName = authority.ownerProfile;
  owner = profiles.${ownerName} or { };
  ownerNames = lib.optional (ownerName != "") ownerName;
  active =
    declarations != { }
    || builtins.any (p: selected p "forgejo-to-kanban") (builtins.attrValues profiles);
  workflow = { inherit (authority) board; };
  hermesReview = authority.reviewExecutor == "hermes";
  origin =
    endpoint: lib.removeSuffix "/api/v2" (lib.removeSuffix "/api/v1" (lib.removeSuffix "/" endpoint));
  projectors = lib.filterAttrs (
    _: p: selected p "kanban-to-vikunja" || selected p "vikunja-to-kanban"
  ) profiles;
  projectorNames = builtins.attrNames projectors;
  projector = if projectorNames != [ ] then projectors.${builtins.head projectorNames} else { };
  projectionSettings =
    if projectorNames == [ ] then
      { }
    else
      settings projector (
        if selected projector "kanban-to-vikunja" then "kanban-to-vikunja" else "vikunja-to-kanban"
      );
  scopeContains =
    p: repository:
    let
      scope = p.externalServices.forgejo.scope.include or null;
    in
    builtins.isList scope && builtins.elem repository scope;
  contracts = lib.mapAttrs (_: source: {
    schema_version = 1;
    review_executor = authority.reviewExecutor;
    id = source.id;
    inherit (source)
      repository
      namespace
      roles
      tenant
      ;
    default_branch = source.defaultBranch;
    workflow_key_prefix = source.workflowKeyPrefix;
    automation_authors = source.automationAuthors;
    board = workflow.board;
    forgejo_origin = origin owner.externalServices.forgejo.endpoint;
    repository_job = source.repositoryJob;
    legacy_container_marker = source.legacyContainerMarker;
    legacy_generations = source.legacyGenerations;
    projection =
      if projectorNames == [ ] then
        null
      else
        {
          origin = origin projector.externalServices.vikunja.endpoint;
          project = projectionSettings.humanActionProject;
          title = projectionSettings.humanActionProjectTitle;
          owner = projectionSettings.projectOwner;
        };
  }) declarations;
  files = lib.mapAttrs (
    name: value: pkgs.writeText "hermes-repository-${name}.json" (builtins.toJSON value)
  ) contracts;
  unique = values: builtins.length values == builtins.length (lib.unique values);
  required = [
    "id"
    "repository"
    "defaultBranch"
    "namespace"
    "workflowKeyPrefix"
    "roles"
    "tenant"
    "automationAuthors"
    "repositoryJob"
    "legacyContainerMarker"
    "legacyGenerations"
    "stateSubdirectory"
  ];
in
{
  options.custom.services.hermes = {
    repositoryAuthority = lib.mkOption {
      default = { };
      description = "Repository authority shared by workflow adapters and persistent agents; independent of Kanban activation.";
      type = lib.types.submodule {
        options = {
          ownerProfile = lib.mkOption {
            type = lib.types.str;
            default = "";
          };
          board = lib.mkOption {
            type = lib.types.str;
            default = "";
          };
          reviewExecutor = lib.mkOption {
            type = lib.types.enum [
              "hermes"
              "paperclip"
            ];
            default = "hermes";
            description = "Hermes profile or Paperclip task agent performs independent reviews.";
          };
          repositories = lib.mkOption {
            type = lib.types.attrsOf (
              lib.types.submodule {
                options =
                  (lib.genAttrs [
                    "id"
                    "repository"
                    "defaultBranch"
                    "namespace"
                    "workflowKeyPrefix"
                    "tenant"
                    "stateSubdirectory"
                  ] (_: lib.mkOption { type = lib.types.str; }))
                  // {
                    roles = lib.mkOption { type = lib.types.attrsOf lib.types.str; };
                    automationAuthors = lib.mkOption { type = lib.types.listOf lib.types.str; };
                    repositoryJob = lib.mkOption { type = lib.types.nullOr lib.types.str; };
                    legacyContainerMarker = lib.mkOption { type = lib.types.nullOr lib.types.str; };
                    legacyGenerations = lib.mkOption { type = lib.types.attrs; };
                  };
              }
            );
            default = { };
          };
        };
      };
    };
    maintenanceRepositoryInstance = lib.mkOption {
      type = lib.types.str;
      description = "Repository instance whose flake composes the deployed fleet used by maintenance inventory.";
      default = "";
    };
    repositoryInstances = lib.mkOption {
      type = lib.types.attrs;
      default = contracts;
      readOnly = true;
      internal = true;
    };
    repositoryInstanceFiles = lib.mkOption {
      type = lib.types.attrsOf lib.types.path;
      default = files;
      readOnly = true;
      internal = true;
    };
    repositoryStateSubdirectories = lib.mkOption {
      type = lib.types.attrsOf lib.types.str;
      default = lib.mapAttrs (_: d: d.stateSubdirectory) declarations;
      readOnly = true;
      internal = true;
    };
    repositoryInstancesDirectory = lib.mkOption {
      type = lib.types.path;
      default = pkgs.runCommand "hermes-repository-instances" { } (
        "mkdir -p $out\n"
        + lib.concatStringsSep "\n" (lib.mapAttrsToList (name: path: "cp ${path} $out/${name}.json") files)
      );
      readOnly = true;
      internal = true;
    };
  };
  config = lib.mkIf active {
    assertions = [
      {
        assertion = builtins.length ownerNames == 1 && declarations != { } && (owner.enable or false);
        message = "Repository adapters require one owning profile and explicit repository declarations.";
      }
      {
        assertion =
          unique (map (d: d.repository) (builtins.attrValues declarations))
          && unique (map (d: d.id) (builtins.attrValues declarations))
          && unique (map (d: d.namespace) (builtins.attrValues declarations))
          && unique (map (d: d.stateSubdirectory) (builtins.attrValues declarations));
        message = "Repository identities, namespaces and state directories must not collide.";
      }
      {
        assertion = builtins.all (
          p:
          !(selected p "container-image-maintenance" || selected p "repository-docs-audit")
          || (
            builtins.hasAttr cfg.maintenanceRepositoryInstance declarations
            && declarations.${cfg.maintenanceRepositoryInstance}.repositoryJob != null
          )
        ) (builtins.attrValues profiles);
        message = "Maintenance must explicitly select the deployment repository and its reference-cache job.";
      }
      {
        assertion = builtins.all (
          p:
          builtins.all
            (
              integration:
              !(selected p integration)
              || (
                ((settings p integration).humanActionProject or null)
                == (projectionSettings.humanActionProject or null)
                && ((settings p integration).projectOwner or null) == (projectionSettings.projectOwner or null)
                &&
                  ((settings p integration).humanActionProjectTitle or null)
                  == (projectionSettings.humanActionProjectTitle or null)
                &&
                  origin (p.externalServices.vikunja.endpoint or "")
                  == origin (projector.externalServices.vikunja.endpoint or "")
                && builtins.elem (toString ((settings p integration).humanActionProject or 0)) (
                  p.externalServices.vikunja.scope.include or [ ]
                )
              )
            )
            [
              "kanban-to-vikunja"
              "vikunja-to-kanban"
            ]
        ) (builtins.attrValues profiles);
        message = "Projection participants must agree on the human target, endpoint and explicit credential scope.";
      }
    ]
    ++ lib.mapAttrsToList (name: d: {
      assertion =
        builtins.attrNames d == lib.sort builtins.lessThan required
        && builtins.match "[a-zA-Z0-9][a-zA-Z0-9_.-]*" name != null
        && (
          d.stateSubdirectory == ""
          || builtins.match "/repositories/[a-zA-Z0-9][a-zA-Z0-9_.-]*" d.stateSubdirectory != null
        )
        && builtins.match "[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*" d.repository != null
        && builtins.match "[A-Za-z0-9][A-Za-z0-9_.-]*" d.id != null
        && builtins.match "[A-Za-z0-9][A-Za-z0-9_.-]*" d.namespace != null
        && d.defaultBranch != ""
        && workflow.board != ""
        && builtins.isList d.automationAuthors
        && builtins.isAttrs d.legacyGenerations
        && builtins.all (
          p:
          !(
            selected p "forgejo-event-ingest"
            || selected p "forgejo-to-kanban"
            || selected p "forgejo-pr-lifecycle"
          )
          || (
            scopeContains p d.repository
            &&
              origin (p.externalServices.forgejo.endpoint or "")
              == origin (owner.externalServices.forgejo.endpoint or "")
          )
        ) (builtins.attrValues profiles)
        && d.roles.implementer == ownerName
        && d.roles.reviewer != ownerName
        && scopeContains owner d.repository
        && (
          if hermesReview then
            (profiles.${d.roles.reviewer}.enable or false)
            && scopeContains profiles.${d.roles.reviewer} d.repository
            &&
              origin (profiles.${d.roles.reviewer}.externalServices.forgejo.endpoint or "")
              == origin (owner.externalServices.forgejo.endpoint or "")
          else
            builtins.hasAttr ownerName cfg.paperclip.profiles
            && !(profiles.${d.roles.reviewer}.enable or false)
            && !(selected owner "forgejo-to-kanban")
        )
        && builtins.all (
          p:
          builtins.all
            (
              integration:
              !(selected p integration) || ((settings p integration).board or workflow.board) == workflow.board
            )
            [
              "kanban-to-vikunja"
              "vikunja-to-kanban"
            ]
        ) (builtins.attrValues profiles);
      message = "Repository ${name} has invalid authority, state routing or profile scope.";
    }) declarations;
  };
}
