# Shared by the immutable helper runtime and its build-time checks.
{ pkgs, skills }:
if skills ? mealie-api then
  pkgs.python3.withPackages (ps: [
    ps.jsonschema
    ps.rfc3339-validator
  ])
else
  pkgs.python3
