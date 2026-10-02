#!/usr/bin/env bash
# Own only our per-profile registry link; never delete repository data.
set -euo pipefail
home=$1
profile=$2
target=$3
registry="$home/job-config/repository-sync/registry.json"

# Reject symlink ancestors before following any profile-relative path.
parent="$home/job-config/repository-sync"
while [ "$parent" != / ]; do
  if [ -L "$parent" ]; then
    # An unselected profile's private directories are not ours to inspect or change.
    if [ -z "$target" ]; then exit 0; fi
    printf '%s\n' 'error: symlink in repository registry parent' >&2
    exit 1
  fi
  parent=$(dirname -- "$parent")
done
owned=false
if [ -L "$registry" ]; then
  case "$(readlink -- "$registry")" in
    /nix/store/*-hermes-"$profile"-repository-sync-registry.json) owned=true ;;
  esac
fi
if [ -z "$target" ]; then
  if "$owned"; then rm -- "$registry"; fi
  exit 0
fi
case "$target" in
  /nix/store/*-hermes-"$profile"-repository-sync-registry.json) ;;
  *) printf '%s\n' 'error: unexpected managed repository registry target' >&2; exit 1 ;;
esac
if { [ -e "$registry" ] || [ -L "$registry" ]; } && ! "$owned"; then
  printf '%s\n' 'error: refusing to overwrite an unmanaged repository registry' >&2
  exit 1
fi
install -d -m 0700 -o "$(stat -c %u -- "$home")" -g "$(stat -c %g -- "$home")" \
  "$home/job-config" "$home/job-config/repository-sync"
staging=$(mktemp -d "$home/job-config/repository-sync/.registry.XXXXXXXX")
trap 'rm -f -- "$staging/registry.json"; rmdir -- "$staging"' EXIT
ln -s -- "$target" "$staging/registry.json"
mv -Tf -- "$staging/registry.json" "$registry"
