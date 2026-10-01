# Provider requirements for the keyvault module.
#
# WHY a module declares its own requirements: `terraform test` inside this
# directory has no parent module to inherit from, so an undeclared provider
# fails with `Error: unknown provider registry.terraform.io/hashicorp/azurerm`
# and the module contract test never runs. The pin also keeps the module on the
# same major as infra/terraform/versions.tf.

terraform {
  required_version = ">= 1.9.0"

  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 4.0"
    }
  }
}
