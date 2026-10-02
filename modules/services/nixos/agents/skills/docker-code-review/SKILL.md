---
name: docker-code-review
description: Use when reviewing Dockerfiles, Compose files, container runtime configuration, images, volumes, or containerized services.
version: 1.0.0
metadata:
  hermes:
    tags: [docker, containers, code-review]
    related_skills: [code-reviewer-review-process]
---

# Docker Code Review

## Criteria

1. Check image provenance, tag/digest pinning policy, update behavior,
   multi-stage build hygiene, build context size, and reproducibility.
2. Check runtime security: user, capabilities, privileged mode, read-only
   filesystems, device mounts, network exposure, secrets, and writable
   state directories.
3. Check volumes and persistence: backup boundaries, ownership, migration
   paths, data-loss risk, and host/container path assumptions.
4. Check health checks, restart policy, logs, resource limits, dependency
   ordering, graceful shutdown, and upgrade/rollback behavior.
5. Check ports, networks, environment variables, and service discovery do
   not expose more than intended.
