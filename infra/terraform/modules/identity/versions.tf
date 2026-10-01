# Provider requirements for the identity module.
#
# WHY a module declares its own requirements: without this block Terraform has
# no provider requirement to resolve when the module is the root of a run, so
# `terraform test` inside this directory dies with
#   Error: unknown provider registry.terraform.io/hashicorp/azurerm
# and the module's own contract tests cannot run at all. Declaring them also
# pins the module to the same major as infra/terraform/versions.tf: an
# undeclared module resolved azurerm 5.7.0 locally while the root pinned
# ~> 4.0, which is a silent split-brain between two copies of the same config.
#
# azuread is required here, not optional: the Entra application registration is
# a Microsoft Graph resource that azurerm cannot express.

terraform {
  required_version = ">= 1.9.0"

  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 4.0"
    }
    azuread = {
      source  = "hashicorp/azuread"
      version = "~> 3.0"
    }
  }
}
