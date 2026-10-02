{ lib }:
let
  capabilityLib = import ./lib.nix { inherit lib; };

  repositoryKnowledge = capabilityLib.mkAgentSkill "repository-knowledge" {
    provides.skills.repository-knowledge = {
      category = "devops";
      source = ../skills/repository-knowledge;
    };
  };
  nixMaintenanceProtocol = capabilityLib.mkAgentSkill "nix-maintenance-protocol" {
    requires = [ repositoryKnowledge.id ];
    provides.skills.nix-maintenance-protocol = {
      category = "devops";
      source = ../skills/nix-maintenance-protocol;
    };
  };
  legacyPrLifecycle = capabilityLib.mkAgentSkill "nix-config-pr-lifecycle" {
    requires = [ forgejoPrLifecycle.id ];
    provides.skills.nix-config-pr-lifecycle = {
      category = "devops";
      source = ../skills/nix-config-pr-lifecycle;
    };
  };
  legacyMaintenanceProtocol = capabilityLib.mkAgentSkill "nix-config-maintenance-protocol" {
    requires = [ nixMaintenanceProtocol.id ];
    provides.skills.nix-config-maintenance-protocol = {
      category = "devops";
      source = ../skills/nix-config-maintenance-protocol;
    };
  };
  forgejoPrLifecycle = capabilityLib.mkAgentSkill "forgejo-pr-lifecycle" {
    requires = [ repositoryKnowledge.id ];
    requiredHermesCapabilities = [ "kanban" ];
    provides.skills.forgejo-pr-lifecycle = {
      category = "devops";
      source = ../skills/forgejo-pr-lifecycle;
    };
  };
  nixManagedResourceChanges = capabilityLib.mkAgentSkill "nix-managed-resource-changes" {
    requiredHermesCapabilities = [ "kanban" ];
    provides.skills.nix-managed-resource-changes = {
      category = "repo-managed";
      source = ../skills/nix-managed-resource-changes;
    };
  };

  evidenceDrivenChangeControl = capabilityLib.mkAgentSkill "evidence-driven-change-control" {
    provides.skills.evidence-driven-change-control = {
      category = "devops";
      source = ../skills/evidence-driven-change-control;
    };
  };

  profileScopedServiceCredentials = capabilityLib.mkAgentSkill "profile-scoped-service-credentials" {
    requires = [ evidenceDrivenChangeControl.id ];
    provides.skills.profile-scoped-service-credentials = {
      category = "devops";
      source = ../skills/profile-scoped-service-credentials;
    };
  };

  securitySensitiveProtocolReview = capabilityLib.mkAgentSkill "security-sensitive-protocol-review" {
    requires = [ evidenceDrivenChangeControl.id ];
    provides.skills.security-sensitive-protocol-review = {
      category = "software-development";
      source = ../skills/security-sensitive-protocol-review;
    };
  };

  declarativeAgentCapabilityPackaging =
    capabilityLib.mkAgentSkill "declarative-agent-capability-packaging"
      {
        requires = [
          evidenceDrivenChangeControl.id
          profileScopedServiceCredentials.id
          securitySensitiveProtocolReview.id
        ];
        provides.skills.declarative-agent-capability-packaging = {
          category = "devops";
          source = ../skills/declarative-agent-capability-packaging;
        };
      };
in
{
  inherit
    declarativeAgentCapabilityPackaging
    evidenceDrivenChangeControl
    profileScopedServiceCredentials
    securitySensitiveProtocolReview
    ;

  all = [
    repositoryKnowledge
    nixMaintenanceProtocol
    legacyPrLifecycle
    legacyMaintenanceProtocol
    forgejoPrLifecycle
    nixManagedResourceChanges
    evidenceDrivenChangeControl
    profileScopedServiceCredentials
    securitySensitiveProtocolReview
    declarativeAgentCapabilityPackaging
  ];
}
