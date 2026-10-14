# Follow-up work

- [ ] Review Paperclip skill management alongside this repository's managed
  agent capabilities. Decide whether reusable skills and publishing tools belong
  here or in a dedicated skills repository. Consider explicit `just` commands
  for inspecting changes, publishing changed skill content through Paperclip's
  native API, and reconciling selected agent assignments without removing their
  existing skills. Keep all API calls independent of Nix evaluation, builds and
  activation. Review operator credential handling, version pinning, drift and
  conflict reporting, and Celine-first runtime validation before wider rollout.
  The immediate use case is native HTTPS artifact delivery for Paperclip's SSH
  workers; runtime credentials remain separate from skill content. For now
  Paperclip is the source of truth for skill content; do not add a
  second repository copy without an explicit publishing and ownership contract.
  `nix-config` onboarding references the native company skill by identity.
