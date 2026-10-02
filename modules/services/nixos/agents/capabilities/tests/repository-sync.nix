{ lib, pkgs }:
let
  # Capture writeText contents without IFD; activation gets separate shell tests.
  testPkgs = pkgs // {
    writeText = name: text: {
      outPath = "/nix/store/test-${name}";
      inherit text;
    };
  };
  settings = {
    sshHost = "git.example.test";
    knownHosts = "git.example.test ssh-ed25519 fixture\n";
    repositories.omanix-pull = {
      origin = "ssh://git@git.example.test/example/omanix.git";
      repository = "example/omanix";
      ref = "refs/heads/main";
    };
  };
  args = {
    inherit lib settings;
    pkgs = testPkgs;
    name = "fixture";
    selectedJobs = [ "omanix-pull" ];
    profileHome = "/profiles/fixture";
    workspaceDir = "/profiles/fixture/workspace";
    identityFile = "/run/fixture/id_ed25519";
  };
  render = overrides: import ../repository-sync-registry.nix (args // overrides);
  data = builtins.fromJSON (render { }).registry.text;
  succeeds =
    overrides: (builtins.tryEval (builtins.deepSeq (render overrides).registry true)).success;
in
assert data.schema_version == 1;
assert builtins.attrNames data.jobs == [ "omanix-pull" ];
assert data.jobs.omanix-pull.destination == "/profiles/fixture/workspace/repositories/omanix";
assert data.jobs.omanix-pull.identity_file == "/run/fixture/id_ed25519";
assert data.jobs.omanix-pull.origin == settings.repositories.omanix-pull.origin;
assert data.jobs.omanix-pull.ref == "refs/heads/main";
assert data.jobs.omanix-pull.mode == "immutable-reference-cache";
assert
  (render {
    settings = { };
    selectedJobs = [ ];
  }).registry == null;
assert !(succeeds { selectedJobs = [ ]; });
assert !(succeeds { identityFile = null; });
assert
  !(succeeds {
    settings = settings // {
      knownHosts = "wrong.example.test ssh-ed25519 fixture\n";
    };
  });
assert
  !(succeeds {
    settings = settings // {
      sshHost = "other.example.test";
    };
  });
assert
  !(succeeds {
    settings = settings // {
      unexpected = true;
    };
  });
true
