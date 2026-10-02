{
  config,
  lib,
  pkgs,
  ...
}:
let
  inherit (lib)
    escapeShellArgs
    mkEnableOption
    mkIf
    mkMerge
    mkOption
    types
    ;
  cfg = config.custom.services.monitoring.prometheusIncidentReconciliation;
  hermesCfg = config.custom.services.hermes;
  hermesProfiles = lib.mapAttrs (
    name: profile: lib.recursiveUpdate profile (config.services.hermes-agent.profiles.${name} or { })
  ) hermesCfg.profiles;
  capabilityLib = import ../agents/capabilities/lib.nix { inherit lib; };

  package = pkgs.hermes-prometheus-reconciler;
  prometheusIncidentReconciliation =
    capabilityLib.mkServiceIntegration "prometheus-incident-reconciliation"
      {
        safety = {
          credentialRequirements = [
            {
              service = "prometheus";
              field = "credential";
            }
          ];
          pathRequirements = [
            {
              path = "/var/lib/hermes-prometheus-incidents";
              access = "read-write";
            }
          ];
          writeScope = "external-system";
          confirmation = "policy-preauthorized";
          verification = "external-readback-required";
        };
        requiredExternalServices = [ "prometheus" ];
        requiredHermesCapabilities = [ "kanban" ];
        provides.runtimePackages = [ package ];
        provides.managedArtifacts = [
          "package:hermes-prometheus-reconciler"
          "unit:prometheus-kanban-reconciler"
          "timer:prometheus-kanban-reconciler"
          "unit:prometheus-incident-control-plane-watchdog"
          "timer:prometheus-incident-control-plane-watchdog"
        ];
      };
  prometheusIncidentReconciliationEnabled = cfg.reconciler.enable || cfg.watchdog.enable;
  prometheusIncidentReconciliationSelectingProfiles = builtins.attrNames (
    lib.filterAttrs (
      _name: profile:
      profile.enable
      && (
        let
          selection = profile.serviceIntegrations."prometheus-incident-reconciliation" or false;
        in
        if builtins.isBool selection then selection else selection.enable
      )
    ) hermesProfiles
  );
  prometheusIncidentReconciliationSelected = prometheusIncidentReconciliationSelectingProfiles != [ ];
  hermesExecutable = "${config.services.hermes-agent.package}/bin/hermes";
  metricsWriterGroup = "prom-incident-metrics-writers";
  watchdogUser = "prometheus-incident-watchdog";
  metricsDirectories = lib.unique (
    lib.optionals cfg.reconciler.enable [ (builtins.dirOf cfg.reconciler.metricsFile) ]
    ++ lib.optionals cfg.watchdog.enable [ (builtins.dirOf cfg.watchdog.metricsFile) ]
  );
  commonArguments = [
    "--prometheus-url"
    cfg.prometheusUrl
    "--prometheus-username-file"
    cfg.prometheusUsernameFile
    "--prometheus-password-file"
    cfg.prometheusPasswordFile
    "--hermes"
    hermesExecutable
    "--board"
    cfg.board
    "--tenant"
    cfg.tenant
    "--assignee"
    cfg.assignee
  ];
  serviceHardening = metricsFile: {
    User = hermesCfg.user;
    Group = hermesCfg.group;
    SupplementaryGroups = [ metricsWriterGroup ];
    UMask = "0027";
    NoNewPrivileges = true;
    PrivateTmp = true;
    PrivateDevices = true;
    ProtectSystem = "strict";
    ProtectHome = true;
    ProtectKernelTunables = true;
    ProtectKernelModules = true;
    ProtectControlGroups = true;
    RestrictSUIDSGID = true;
    LockPersonality = true;
    RestrictRealtime = true;
    SystemCallArchitectures = "native";
    RestrictAddressFamilies = [
      "AF_INET"
      "AF_INET6"
      "AF_UNIX"
    ];
    StateDirectory = "hermes-prometheus-incidents";
    ReadWritePaths = [
      (builtins.dirOf metricsFile)
      "${hermesCfg.stateDir}/.hermes"
    ];
  };
  commonEnvironment = {
    HOME = hermesCfg.stateDir;
    HERMES_HOME = "${hermesCfg.stateDir}/.hermes";
  };
  watchdogHardening = metricsFile: {
    User = watchdogUser;
    Group = metricsWriterGroup;
    UMask = "0027";
    NoNewPrivileges = true;
    PrivateTmp = true;
    PrivateDevices = true;
    ProtectSystem = "strict";
    ProtectHome = true;
    ProtectKernelTunables = true;
    ProtectKernelModules = true;
    ProtectControlGroups = true;
    RestrictSUIDSGID = true;
    LockPersonality = true;
    RestrictRealtime = true;
    SystemCallArchitectures = "native";
    RestrictAddressFamilies = [
      "AF_INET"
      "AF_INET6"
      "AF_UNIX"
    ];
    ReadWritePaths = [ (builtins.dirOf metricsFile) ];
  };
in
{
  options.custom.services.monitoring.prometheusIncidentReconciliation = {
    metricsDirectory = mkOption {
      type = types.str;
      description = "Configured host textfile collector directory.";
    };
    prometheusUrl = mkOption {
      type = types.str;
      default = "";
      description = "Canonical Prometheus HTTP API base URL.";
    };

    prometheusUsernameFile = mkOption {
      type = types.str;
      default = "";
      description = "Runtime file containing the least-privilege Prometheus username.";
    };

    prometheusPasswordFile = mkOption {
      type = types.str;
      default = "";
      description = "Runtime file containing the least-privilege Prometheus password.";
    };

    board = mkOption {
      type = types.str;
      default = "homelab-devops";
      description = "Explicit native Hermes Kanban board.";
    };

    tenant = mkOption {
      type = types.str;
      default = "homelab-devops";
      description = "Kanban tenant namespace for machine incidents.";
    };

    assignee = mkOption {
      type = types.str;
      default = "system-admin";
      description = "Existing Hermes profile assigned to machine-incident cards.";
    };

    ownerProfile = mkOption {
      type = types.str;
      default = "system-admin";
      description = "The single enabled Hermes profile allowed to select this host-wide integration.";
    };

    reconciler = {
      enable = mkEnableOption "deterministic Prometheus incident reconciliation into native Hermes Kanban";

      interval = mkOption {
        type = types.str;
        default = "1m";
        description = "Reconciliation timer interval.";
      };

      metricsFile = mkOption {
        type = types.str;
        default = "/var/lib/node-exporter/textfile/hermes-prometheus-reconciler.prom";
        description = "Node-exporter textfile path for fixed label-free reconciler metrics.";
      };
    };

    watchdog = {
      enable = mkEnableOption "external narrow Prometheus/reconciler control-plane watchdog";

      prometheusUsernameFile = mkOption {
        type = types.str;
        default = "";
        description = "Runtime file containing the watchdog's least-privilege Prometheus username.";
      };

      prometheusPasswordFile = mkOption {
        type = types.str;
        default = "";
        description = "Runtime file containing the watchdog's least-privilege Prometheus password.";
      };

      interval = mkOption {
        type = types.str;
        default = "2m";
        description = "External watchdog timer interval.";
      };

      metricsFile = mkOption {
        type = types.str;
        default = "/var/lib/node-exporter/textfile/hermes-prometheus-control-plane-watchdog.prom";
        description = "Node-exporter textfile path for fixed label-free watchdog metrics.";
      };

      reconcilerMaxAgeSeconds = mkOption {
        type = types.ints.between 60 3600;
        default = 180;
        description = "Maximum age of the reconciler success metric before the watchdog opens an outage.";
      };

    };
  };

  config = mkMerge [
    {
      custom.services.hermes.capabilityPackages.${prometheusIncidentReconciliation.id} =
        prometheusIncidentReconciliation;
      assertions = [
        {
          assertion = !prometheusIncidentReconciliationSelected || prometheusIncidentReconciliationEnabled;
          message = "Prometheus incident reconciliation selected by an enabled Hermes profile must enable a standing integration.";
        }
        {
          assertion =
            !prometheusIncidentReconciliationEnabled
            || prometheusIncidentReconciliationSelectingProfiles == [ cfg.ownerProfile ];
          message = "Prometheus incident reconciliation must be selected by exactly its declared owner profile.";
        }
      ];
    }
    (mkIf prometheusIncidentReconciliationEnabled {
      assertions = [
        {
          assertion = !cfg.watchdog.enable || !(config.services.prometheus.enable or false);
          message = "The external Prometheus watchdog must run outside Prometheus's own failure domain.";
        }
        {
          assertion = !cfg.reconciler.enable || hermesCfg.enable;
          message = "Prometheus incident reconciliation requires the repo-managed Hermes service and native Kanban CLI.";
        }
        {
          assertion = prometheusIncidentReconciliationSelected;
          message = "Prometheus incident reconciliation must be selected by an enabled Hermes profile.";
        }
        {
          assertion = builtins.match "^https?://.+" cfg.prometheusUrl != null;
          message = "prometheusIncidentReconciliation.prometheusUrl must be an HTTP(S) URL.";
        }
        {
          assertion =
            !cfg.reconciler.enable
            || (lib.hasPrefix "/" cfg.prometheusUsernameFile && lib.hasPrefix "/" cfg.prometheusPasswordFile);
          message = "Prometheus reconciler credential files must be absolute runtime paths.";
        }
        {
          assertion =
            !cfg.watchdog.enable
            || (
              lib.hasPrefix "/" cfg.watchdog.prometheusUsernameFile
              && lib.hasPrefix "/" cfg.watchdog.prometheusPasswordFile
            );
          message = "Prometheus watchdog credential files must be absolute runtime paths.";
        }
        {
          assertion =
            lib.hasPrefix "/" cfg.reconciler.metricsFile && lib.hasPrefix "/" cfg.watchdog.metricsFile;
          message = "Prometheus incident reconciliation metrics files must be absolute.";
        }
        {
          assertion = cfg.metricsDirectory != "";
          message = "Prometheus incident reconciliation requires the host exporter textfile collector.";
        }
        {
          assertion = lib.all (directory: directory == cfg.metricsDirectory) metricsDirectories;
          message = "Prometheus incident reconciliation metrics must use the configured host-exporter textfile directory.";
        }

      ];

      users.groups.${metricsWriterGroup} = { };
      users.users.${watchdogUser} = mkIf cfg.watchdog.enable {
        isSystemUser = true;
        group = metricsWriterGroup;
      };
    })

    (mkIf cfg.reconciler.enable {
      systemd.services.prometheus-kanban-reconciler = {
        description = "Reconcile actionable Prometheus alerts into native Hermes Kanban";
        after = [ "network-online.target" ];
        wants = [ "network-online.target" ];
        environment = commonEnvironment;
        serviceConfig = (serviceHardening cfg.reconciler.metricsFile) // {
          Type = "oneshot";
          ExecStart = escapeShellArgs (
            [
              "${package}/bin/hermes-prometheus-reconciler"
              "reconcile"
              "--state-dir"
              "/var/lib/hermes-prometheus-incidents/reconciler"
              "--metrics-file"
              cfg.reconciler.metricsFile
            ]
            ++ commonArguments
          );
        };
      };

      systemd.timers.prometheus-kanban-reconciler = {
        description = "Deterministic Prometheus incident reconciliation schedule";
        wantedBy = [ "timers.target" ];
        timerConfig = {
          OnBootSec = "2m";
          OnUnitActiveSec = cfg.reconciler.interval;
          AccuracySec = "10s";
          Persistent = true;
          Unit = "prometheus-kanban-reconciler.service";
        };
      };
    })

    (mkIf cfg.watchdog.enable {
      systemd.services.prometheus-incident-control-plane-watchdog = {
        description = "External narrow watchdog for the Prometheus incident control plane";
        after = [ "network-online.target" ];
        wants = [ "network-online.target" ];
        serviceConfig = watchdogHardening cfg.watchdog.metricsFile // {
          Type = "oneshot";
          ExecStart = escapeShellArgs [
            "${package}/bin/hermes-prometheus-reconciler"
            "external-watchdog"
            "--metrics-file"
            cfg.watchdog.metricsFile
            "--prometheus-url"
            cfg.prometheusUrl
            "--prometheus-username-file"
            cfg.watchdog.prometheusUsernameFile
            "--prometheus-password-file"
            cfg.watchdog.prometheusPasswordFile
            "--reconciler-max-age-seconds"
            (toString cfg.watchdog.reconcilerMaxAgeSeconds)
          ];
        };
      };

      systemd.timers.prometheus-incident-control-plane-watchdog = {
        description = "External Prometheus incident control-plane watchdog schedule";
        wantedBy = [ "timers.target" ];
        timerConfig = {
          OnBootSec = "3m";
          OnUnitActiveSec = cfg.watchdog.interval;
          AccuracySec = "10s";
          Persistent = true;
          Unit = "prometheus-incident-control-plane-watchdog.service";
        };
      };
    })
  ];
}
