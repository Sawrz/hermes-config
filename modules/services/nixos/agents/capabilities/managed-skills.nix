# Published sources retain actionable repository ownership across store copies.
{ lib, repositories }:
let
  locate =
    path:
    let
      value = builtins.unsafeDiscardStringContext (toString path);
      matches = lib.filterAttrs (_: entry: lib.hasPrefix "${toString entry.root}/" value) repositories;
      names = builtins.attrNames matches;
    in
    assert lib.assertMsg (builtins.length names <= 1) "Managed source roots overlap";
    if names == [ ] then
      null
    else
      let
        entry = matches.${builtins.head names};
      in
      {
        path = lib.removePrefix "${toString entry.root}/" value;
        inherit (entry) repository;
        route = builtins.removeAttrs entry [ "root" ];
      };
  declaration =
    path:
    let
      found = locate path;
    in
    if found != null then
      found
    else
      {
        path = null;
        repository = null;
        packaged_input = builtins.unsafeDiscardStringContext (toString path);
      };
in
{
  manifest =
    {
      profile,
      skills,
      providers,
      nativeJobs ? { },
      selectionDeclarations ? [ ],
    }:
    {
      schema_version = 2;
      inherit profile;
      profile_declarations = map declaration (lib.unique selectionDeclarations);
      native_jobs = lib.mapAttrs (name: job: {
        provider = providers."cron:${name}";
        declarations = map declaration (lib.unique (job.declarationFiles or [ ]));
        source = {
          kind = "native-job";
          path = null;
        };
      }) nativeJobs;
      skills = lib.mapAttrs (name: skill: {
        inherit (skill) category;
        provider = providers."skill:${name}" or "declarativeSkills";
        declarations = map declaration (lib.unique (skill.declarationFiles or [ ]));
        source =
          if skill.source == null then
            {
              kind = "inline";
              path = null;
            }
          else
            let
              found = locate skill.source;
            in
            if found != null then
              found // { kind = "repository-directory"; }
            else
              {
                kind = "external";
                path = null;
                packaged_input = builtins.unsafeDiscardStringContext (toString skill.source);
              };
      }) skills;
    };
}
