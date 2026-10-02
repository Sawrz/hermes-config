{
  config,
  lib,
  pkgs,
  ...
}:
let
  cfg = config.custom.services.hermes.paperclip;
  hermes = config.custom.services.hermes;
  names = builtins.attrNames cfg.profiles;
  ports = map (name: cfg.profiles.${name}.port) names;
  secret = name: cfg.profiles.${name}.secretName;
  template = name: "hermes-paperclip-${name}-env";
  unit = name: "hermes-agent-${name}.service";
  onboardingDir = name: "/run/user/${toString hermes.uid}/hermes-paperclip/${name}/onboarding";
  onboardingMount = name: "${onboardingDir name}:/run/hermes-paperclip/onboarding:ro";
  rules = lib.concatMap (
    port:
    map (address: {
      inherit port address;
    }) cfg.allowedClientAddresses
  ) ports;
  iptablesRule = action: rule: ''
    ${pkgs.iptables}/bin/iptables ${action} nixos-fw \
      -s ${lib.escapeShellArg rule.address} -d ${lib.escapeShellArg cfg.bindAddress} \
      -p tcp --dport ${toString rule.port} -j nixos-fw-accept
  '';
in
{
  options.custom.services.hermes.paperclip = {
    bindAddress = lib.mkOption {
      type = lib.types.strMatching "[0-9]+\\.[0-9]+\\.[0-9]+\\.[0-9]+";
      default = "127.0.0.1";
      description = "Internal IPv4 address for the selected Hermes profile APIs.";
    };
    allowedClientAddresses = lib.mkOption {
      type = lib.types.listOf (lib.types.strMatching "[0-9]+\\.[0-9]+\\.[0-9]+\\.[0-9]+(/[0-9]+)?");
      default = [ ];
      description = "Paperclip server IPv4 addresses/CIDRs allowed through the host firewall.";
    };
    profiles = lib.mkOption {
      default = { };
      description = "Existing named Hermes profiles exposed to Paperclip, each with its own port and SOPS credential.";
      type = lib.types.attrsOf (
        lib.types.submodule (
          { name, ... }: {
            options = {
              port = lib.mkOption {
                type = lib.types.port;
                description = "Dedicated API listener port for this profile.";
              };
              secretName = lib.mkOption {
                type = lib.types.str;
                default = "hermes/paperclip/${name}/credential";
                description = "SOPS secret containing the profile's API_SERVER_KEY.";
              };
            };
          }
        )
      );
    };
  };

  config = lib.mkIf (cfg.profiles != { }) {
    assertions = [
      {
        assertion = hermes.enable && !hermes.multiplexProfiles.enable;
        message = "Hermes Paperclip APIs require enabled Hermes with separate profile gateways.";
      }
      {
        assertion = builtins.all (name: hermes.profiles.${name}.enable or false) names;
        message = "Hermes Paperclip APIs must select existing enabled named profiles.";
      }
      {
        assertion = builtins.length ports == builtins.length (lib.unique ports);
        message = "Hermes Paperclip profiles must use distinct API ports.";
      }
      {
        assertion = builtins.length (lib.unique (map secret names)) == builtins.length names;
        message = "Hermes Paperclip profiles must use distinct API credentials.";
      }
      {
        assertion = config.networking.firewall.enable && cfg.allowedClientAddresses != [ ];
        message = "Hermes Paperclip APIs require an enabled firewall and explicit allowed client addresses.";
      }
    ];

    sops.secrets = lib.listToAttrs (
      map (
        name:
        lib.nameValuePair (secret name) {
          owner = hermes.user;
          group = hermes.group;
          mode = "0400";
          restartUnits = [ (unit name) ];
        }
      ) names
    );
    sops.templates = lib.listToAttrs (
      map (
        name:
        lib.nameValuePair (template name) {
          owner = hermes.user;
          group = hermes.group;
          mode = "0400";
          restartUnits = [ (unit name) ];
          content = ''
            API_SERVER_KEY=${config.sops.placeholder.${secret name}}
          '';
        }
      ) names
    );
    custom.services.hermes.profiles = lib.mapAttrs (name: profile: {
      environment = {
        API_SERVER_ENABLED = "true";
        API_SERVER_HOST = cfg.bindAddress;
        API_SERVER_PORT = toString profile.port;
      };
      environmentFiles = [ config.sops.templates.${template name}.path ];
      extraDockerVolumes = [ (onboardingMount name) ];
    }) cfg.profiles;

    # Each configured profile retains read-only access to its own gateway key.
    # Stage a stable directory so atomic SOPS rotations remain visible inside
    # an existing read-only directory mount. Never forward the gateway env file.
    systemd.services = lib.mapAttrs' (
      name: _profile:
      lib.nameValuePair (lib.removeSuffix ".service" (unit name)) {
        preStart = lib.mkBefore ''
          onboarding_dir=${lib.escapeShellArg (onboardingDir name)}
          ${pkgs.coreutils}/bin/install -d -m 0700 "$onboarding_dir"
          ${pkgs.coreutils}/bin/install -m 0400 \
            ${lib.escapeShellArg config.sops.secrets.${secret name}.path} \
            "$onboarding_dir/.gateway-key.next"
          ${pkgs.coreutils}/bin/mv -f "$onboarding_dir/.gateway-key.next" "$onboarding_dir/gateway-key"
        '';
      }
    ) cfg.profiles;

    networking.firewall = lib.mkMerge [
      (lib.mkIf (!config.networking.nftables.enable) {
        extraCommands = lib.concatMapStringsSep "\n" (iptablesRule "-A") rules;
        extraStopCommands = lib.concatMapStringsSep "\n" (
          rule: lib.removeSuffix "\n" (iptablesRule "-D" rule) + " || true\n"
        ) rules;
      })
      (lib.mkIf config.networking.nftables.enable {
        extraInputRules = lib.concatMapStringsSep "\n" (
          rule: "ip saddr ${rule.address} ip daddr ${cfg.bindAddress} tcp dport ${toString rule.port} accept"
        ) rules;
      })
    ];
  };
}
