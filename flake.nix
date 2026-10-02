{
  description = "Managed Hermes capabilities, native workflow adapters and runtime integration";
  inputs.nixpkgs-unstable.url = "git+ssh://git@ssh.git.wrzalek.com/nixos/upstream-nixpkgs.git?shallow=1&ref=nixos-unstable";
  inputs.nixpkgs.url = "git+ssh://git@ssh.git.wrzalek.com/nixos/upstream-nixpkgs.git?shallow=1&ref=nixos-26.05";
  inputs.sops-nix = {
    url = "git+ssh://git@ssh.git.wrzalek.com/nixos/upstream-sops-nix.git?shallow=1";
    inputs.nixpkgs.follows = "nixpkgs";
  };
  inputs.hermes-agent = {
    url = "git+ssh://git@ssh.git.wrzalek.com/nixos/upstream-hermes-agent.git?shallow=1&ref=refs/tags/v2026.9.14";
    inputs.nixpkgs.follows = "nixpkgs";
  };
  # Keep transitive source reads on the verified Forgejo mirrors.
  inputs.hermes-agent.inputs.flake-parts.url =
    "git+ssh://git@ssh.git.wrzalek.com/nixos/upstream-flake-parts.git?shallow=1";
  inputs.hermes-agent.inputs.home-manager.url =
    "git+ssh://git@ssh.git.wrzalek.com/nixos/upstream-home-manager.git?shallow=1";
  inputs.hermes-agent.inputs.npm-lockfile-fix.url =
    "git+ssh://git@ssh.git.wrzalek.com/nixos/upstream-npm-lockfile-fix.git?shallow=1";
  inputs.hermes-agent.inputs.pyproject-build-systems.url =
    "git+ssh://git@ssh.git.wrzalek.com/nixos/upstream-build-system-pkgs.git?shallow=1";
  inputs.hermes-agent.inputs.pyproject-nix.url =
    "git+ssh://git@ssh.git.wrzalek.com/nixos/upstream-pyproject.nix.git?shallow=1";
  inputs.hermes-agent.inputs.uv2nix.url =
    "git+ssh://git@ssh.git.wrzalek.com/nixos/upstream-uv2nix.git?shallow=1";
  outputs =
    inputs@{ self, nixpkgs, ... }:
    let
      each = nixpkgs.lib.genAttrs [
        "x86_64-linux"
        "aarch64-linux"
      ];
      pkgsFor =
        system:
        import nixpkgs {
          inherit system;
          overlays = [ self.overlays.default ];
        };
    in
    {
      overlays.default = import ./pkgs { inherit inputs; };
      nixosModules.default = {
        imports = [
          ./modules/services/nixos/agents
          inputs.sops-nix.nixosModules.sops
          inputs.hermes-agent.nixosModules.default
        ];
        nixpkgs.overlays = [ self.overlays.default ];
      };
      nixosModules.incident-reconciliation = import ./modules/services/nixos/monitoring/incident-reconciliation.nix;
      packages = each (system: self.overlays.default (pkgsFor system) (pkgsFor system));
      checks = each (
        system:
        let
          pkgs = pkgsFor system;
        in
        self.packages.${system}
        // (import ./checks/components.nix { inherit self pkgs; })
        // (import ./checks/adapters.nix { inherit self pkgs; })
        // (import ./checks/native-cron.nix { inherit self pkgs; })
        // {
          repositories = import ./checks/repositories.nix {
            inherit
              self
              inputs
              pkgs
              system
              ;
          };
          capability-graph = import ./modules/services/nixos/agents/capabilities/check.nix {
            inherit inputs pkgs system;
          };
          sandbox = import ./modules/services/nixos/agents/capabilities/sandbox-check.nix { inherit pkgs; };
        }
      );
    };
}
