# Render only an explicitly selected profile inventory; cadence stays profile-owned.
{
  lib,
  pkgs,
  name,
  settings,
  selectedJobs,
  profileHome,
  workspaceDir,
  identityFile,
}:
let
  inherit (lib) mkOption types;
  schema =
    (lib.evalModules {
      modules = [
        {
          options = {
            sshHost = mkOption {
              type = types.strMatching "[A-Za-z0-9.-]+";
              default = "invalid";
            };
            sshPort = mkOption {
              type = types.port;
              default = 22;
            };
            knownHosts = mkOption {
              type = types.lines;
              default = "";
            };
            repositories = mkOption {
              default = { };
              type = types.attrsOf (
                types.submodule {
                  options = {
                    origin = mkOption { type = types.str; };
                    repository = mkOption { type = types.strMatching "[A-Za-z0-9._-]+/[A-Za-z0-9._-]+"; };
                    ref = mkOption { type = types.strMatching "refs/heads/[A-Za-z0-9._/-]+"; };
                  };
                }
              );
            };
          };
          config = settings;
        }
      ];
    }).config;
  inherit (schema) repositories;
  enabled = repositories != { };
  host = schema.sshHost;
  port = schema.sshPort;
  hostPort = host + lib.optionalString (port != 22) ":${toString port}";
  marker = if port == 22 then host else "[${host}]:${toString port}";
  pinned = builtins.any (line: lib.hasPrefix "${marker} " line) (
    lib.splitString "\n" schema.knownHosts
  );
  valid =
    !enabled
    || (
      identityFile != null
      && pinned
      && builtins.attrNames repositories == lib.sort builtins.lessThan selectedJobs
      && builtins.all (job: builtins.match "[a-z0-9][a-z0-9._-]*-pull" job != null) selectedJobs
      && builtins.all (repo: repo.origin == "ssh://git@${hostPort}/${repo.repository}.git") (
        builtins.attrValues repositories
      )
    );
  knownHostsFile = pkgs.writeText "hermes-${name}-repository-sync-known-hosts" schema.knownHosts;
  registry =
    assert lib.assertMsg valid
      "Hermes ${name} repository-sync requires exact selected jobs, canonical origins, pinned SSH host and a profile identity";
    if !enabled then
      null
    else
      pkgs.writeText "hermes-${name}-repository-sync-registry.json" (
        builtins.toJSON {
          schema_version = 1;
          jobs = lib.mapAttrs (job: repo: {
            inherit (repo) origin repository ref;
            destination = "${workspaceDir}/repositories/${lib.removeSuffix "-pull" job}";
            transport = "ssh";
            mode = "immutable-reference-cache";
            ssh_host = host;
            ssh_port = port;
            known_hosts_file = toString knownHostsFile;
            identity_file = identityFile;
            timeout_seconds = 120;
            retries = 2;
            lock_timeout_seconds = 0;
          }) repositories;
        }
      );
in
{
  inherit registry;
  activation = ''
    PATH=${lib.makeBinPath [ pkgs.coreutils ]}:"$PATH" ${pkgs.bash}/bin/bash \
      ${./activate-repository-registry.sh} \
      ${lib.escapeShellArgs [
        profileHome
        name
        (if registry == null then "" else toString registry)
      ]}
  '';
}
