{ self, pkgs }:
let
  inherit (pkgs) lib;
  inputs = self.inputs // {
    hermes-config = self;
  };
  evaluated = inputs.nixpkgs.lib.nixosSystem {
    system = pkgs.stdenv.hostPlatform.system;
    specialArgs = {
      inherit inputs;
      adminUser = "tester";
      hostname = "hermes-cache-fixture";
      stateVersion = "25.11";
    };
    modules = [
      self.nixosModules.default
      {
        system.stateVersion = "25.11";
        users.users.tester.isNormalUser = true;
        custom.services.hermes = {
          enable = true;
          uid = 989;
          gid = 985;
          stateDir = "/var/lib/cache-fixture";
          defaultProfile.telegram.enable = false;
          defaultProfile.ssh.privateKeyFile = "/test-secrets/default-ssh";
          profiles = {
            lead.telegram.enable = false;
            inactive = {
              enable = false;
              telegram.enable = false;
            };
          };
        };
      }
    ];
  };
  config = evaluated.config;
  cfg = config.custom.services.hermes;
  home = "${cfg.stateDir}/.hermes";
  profiles = builtins.attrNames (lib.filterAttrs (_: profile: profile.enable) cfg.profiles);
  homes = [ home ] ++ map (name: "${home}/profiles/${name}") profiles;
  rules = lib.concatMap (directory: [
    "d ${directory}/cache 0770 ${cfg.user} ${cfg.group} -"
    "d ${directory}/cache/vision 0770 ${cfg.user} ${cfg.group} -"
  ]) homes;
  fixtureRules = pkgs.writeText "hermes-cache-rules" (lib.concatStringsSep "\n" rules);
in
{
  hermes-cache =
    assert builtins.all (rule: builtins.elem rule config.systemd.tmpfiles.rules) rules;
    assert !(builtins.any (lib.hasInfix "/profiles/inactive/cache") config.systemd.tmpfiles.rules);
    assert lib.hasInfix "${home}/cache/vision"
      config.system.activationScripts.hermes-default-profile.text;
    assert lib.hasInfix "${home}/profiles/lead/cache/vision"
      config.system.activationScripts.hermes-profiles.text;
    pkgs.runCommand "hermes-cache"
      {
        nativeBuildInputs = [
          pkgs.python3
          pkgs.systemd
        ];
      }
      ''
        python3 - <<'PY'
        import os
        from pathlib import Path
        import subprocess

        # Exercise the real tmpfiles rules in an isolated tree. The build user
        # substitutes for the production service UID/GID; no host state is used.
        root = Path(os.environ["TMPDIR"]) / "profiles"
        rendered = []
        caches = []
        for line in Path("${fixtureRules}").read_text().splitlines():
            fields = line.split()
            target = root / fields[1].lstrip("/")
            fields[3:5] = [str(os.getuid()), str(os.getgid())]
            rendered.append(" ".join(fields))
            if target.name == "cache":
                target.mkdir(parents=True)
                sentinel = target / "existing-image"
                sentinel.write_text("preserved")
                sentinel.chmod(0o640)
                target.chmod(0o550)
                caches.append((target, sentinel))
        rules = root / "tmpfiles.conf"
        rules.write_text("\n".join(rendered) + "\n")
        for _ in range(2):
            subprocess.run(
                ["systemd-tmpfiles", "--root", str(root), "--create", str(rules)],
                check=True,
            )
            for cache, sentinel in caches:
                assert cache.stat().st_mode & 0o777 == 0o770
                vision = cache / "vision"
                assert vision.stat().st_mode & 0o777 == 0o770
                (vision / "probe").write_text("writable")
                assert sentinel.read_text() == "preserved"
                assert sentinel.stat().st_mode & 0o777 == 0o640
        PY
        touch "$out"
      '';
}
