---
name: nix-nixos-code-review
description: Use when reviewing Nix, NixOS, nix-darwin, flake, module, systemd, or declarative host/service configuration changes.
version: 1.1.0
metadata:
  hermes:
    tags: [nix, nixos, nix-darwin, code-review, infrastructure]
    related_skills: [code-reviewer-review-process]
---

# Nix/NixOS Code Review

## When to Use

Use this for Nix language, NixOS module, nix-darwin, flake, overlay,
package, host, service, generated file, and systemd-related changes.
This skill is repository-agnostic; pair it with optional repo context
when the repository has additional rules.

## Criteria

1. Check expression correctness: syntax, attribute paths, option types,
   defaults, nullability, list merging, string interpolation, escaping,
   and lazy evaluation surprises.
2. Check module behavior: imports, option declarations, mkIf/mkMerge
   conditions, mkDefault/mkForce priority, generated config shape, and
   whether the change belongs in host-local config or a shared module.
3. Compare sibling hosts/modules when shared behavior, naming, or
   placement matters. Avoid premature abstraction, but do not hide a
   shared behavioral fix in one host if siblings need it too.
4. Check operational semantics: activation order, restart triggers,
   systemd unit dependencies, timers, file modes, ownership, state
   directories, rollbacks, rebuild persistence, and noisy versus silent
   failure modes.
5. Check security: least privilege, secret material kept in SOPS/env
   files rather than rendered logs or world-readable files, read-only
   mounts where possible, and narrow service credentials.
6. Check verification evidence: eval, targeted flake checks,
   formatter/check scripts, generated config inspection, and CI status
   appropriate to the touched outputs.
7. Separate repository correctness from live deployment. A successful
   eval/build/check does not prove the host has switched or that live
   verification passed.
