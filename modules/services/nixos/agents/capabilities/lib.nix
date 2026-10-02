{ lib }:
let
  inherit (lib)
    concatMap
    foldl'
    unique
    ;

  safeName = name: builtins.match "^[a-z0-9][a-z0-9._-]*$" name != null;

  managedArtifactValid =
    artifact:
    builtins.match "^(skill|hermes-config|file|unit|timer|cron|manifest|package|runbook|scan|ci):[a-z0-9][a-z0-9._-]*(/[a-z0-9][a-z0-9._-]*)*$" artifact
    != null
    && !(lib.hasInfix ".." artifact)
    && !(lib.hasInfix "//" artifact);

  validateName =
    kind: name: if safeName name then name else throw "Hermes ${kind} name '${name}' is unsafe";

  normalize =
    id: kind: name: attrs:
    let
      normalized = lib.recursiveUpdate {
        inherit id kind name;
        requires = [ ];
        requiredExternalServices = [ ];
        requiredHermesCapabilities = [ ];
        conflicts = [ ];
        targets = [ "hermes" ];
        allowedSettings = [ ];
        safetyDeclared = attrs ? safety;
        safety = {
          credentialRequirements = [ ];
          pathRequirements = [ ];
          writeScope = "none";
          confirmation = "not-required";
          verification = "not-required";
        };
        provides = {
          skills = { };
          toolsets = [ ];
          hermesConfig = { };
          managedArtifacts = [ ];
          nativeJobs = { };
          runtimePackages = [ ];
        };
      } attrs;
    in
    normalized
    // {
      provides = normalized.provides // {
        managedArtifacts = unique (
          normalized.provides.managedArtifacts
          ++ map (jobName: "cron:${jobName}") (builtins.attrNames normalized.provides.nativeJobs)
        );
      };
    };

  resolve =
    packages: roots:
    let
      visit =
        path: id:
        if builtins.elem id path then
          throw "Hermes capability dependency cycle: ${lib.concatStringsSep " -> " (path ++ [ id ])}"
        else if !(builtins.hasAttr id packages) then
          throw "Hermes capability '${id}' is required but has no provider"
        else
          let
            package = packages.${id};
          in
          concatMap (visit (path ++ [ id ])) package.requires ++ [ id ];

      orderedIds = unique (concatMap (visit [ ]) roots);
      selected = map (id: packages.${id}) orderedIds;
      providerPairs = concatMap (
        package:
        map
          (artifact: {
            name = artifact;
            provider = package.id;
          })
          (
            package.provides.managedArtifacts
            ++ map (name: "skill:${name}") (builtins.attrNames package.provides.skills)
            ++ map (name: "hermes-config:${name}") (builtins.attrNames package.provides.hermesConfig)
          )
      ) selected;
      providerNames = map (pair: pair.name) providerPairs;
      duplicateProviders = unique (
        builtins.filter (
          name: builtins.length (builtins.filter (candidate: candidate == name) providerNames) > 1
        ) providerNames
      );
      conflicts = concatMap (
        package:
        map (conflict: "${package.id} conflicts with ${conflict}") (
          builtins.filter (conflict: builtins.elem conflict orderedIds) package.conflicts
        )
      ) selected;
      unsupportedTargets = map (package: package.id) (
        builtins.filter (package: !(builtins.elem "hermes" package.targets)) selected
      );
      invalidArtifacts = builtins.filter (pair: !(managedArtifactValid pair.name)) providerPairs;
      invalidToolsets = builtins.filter (name: !safeName name) (
        concatMap (p: p.provides.toolsets) selected
      );
      skills = foldl' (acc: package: acc // package.provides.skills) { } selected;
      hermesConfig = foldl' lib.recursiveUpdate { } (
        map (package: package.provides.hermesConfig) selected
      );
      nativeJobs = foldl' (acc: package: acc // package.provides.nativeJobs) { } selected;
      runtimePackages = unique (concatMap (package: package.provides.runtimePackages) selected);
      safety = map (package: {
        provider = package.id;
        inherit (package.safety)
          confirmation
          credentialRequirements
          pathRequirements
          verification
          writeScope
          ;
      }) selected;
      provenance = builtins.listToAttrs (
        map (pair: {
          inherit (pair) name;
          value = pair.provider;
        }) providerPairs
      );
    in
    if duplicateProviders != [ ] then
      throw "Hermes capability duplicate managed providers: ${lib.concatStringsSep ", " duplicateProviders}"
    else if conflicts != [ ] then
      throw "Hermes capability unsupported combination: ${lib.concatStringsSep "; " conflicts}"
    else if unsupportedTargets != [ ] then
      throw "Hermes capability packages do not support Hermes: ${lib.concatStringsSep ", " unsupportedTargets}"
    else if invalidArtifacts != [ ] then
      throw "Hermes capability has an unsafe or reserved managed artifact name"
    else if invalidToolsets != [ ] then
      throw "Hermes capability has an unsafe toolset name: ${lib.concatStringsSep ", " invalidToolsets}"
    else
      {
        inherit
          hermesConfig
          nativeJobs
          orderedIds
          provenance
          runtimePackages
          safety
          skills
          ;
        toolsets = unique (concatMap (package: package.provides.toolsets) selected);
        requiredExternalServices = unique (concatMap (package: package.requiredExternalServices) selected);
        requiredHermesCapabilities = unique (
          concatMap (package: package.requiredHermesCapabilities) selected
        );
      };
in
{
  inherit managedArtifactValid resolve safeName;

  mkAgentSkill =
    name: attrs: normalize "agentSkill:${validateName "agent skill" name}" "agentSkill" name attrs;

  mkExternalServiceCapability =
    service: capability: attrs:
    let
      serviceName = validateName "external service" service;
      capabilityName = validateName "external service capability" capability;
    in
    normalize "externalService:${serviceName}:${capabilityName}" "externalService" capabilityName (
      attrs
      // {
        requiredExternalServices = unique ([ serviceName ] ++ (attrs.requiredExternalServices or [ ]));
        safety = {
          credentialRequirements = [
            {
              service = serviceName;
              field = "credential";
            }
          ];
          pathRequirements = [ ];
          writeScope = "none";
          confirmation = "not-required";
          verification = "not-required";
        }
        // (attrs.safety or { });
      }
    );

  mkServiceIntegration =
    name: attrs:
    normalize "serviceIntegration:${validateName "service integration" name}" "serviceIntegration" name
      attrs;
}
