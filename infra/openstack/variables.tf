variable "environment" {
  type        = string
  description = "Deployment environment name."

  validation {
    condition     = contains(["dev", "staging", "prod"], var.environment)
    error_message = "environment must be one of: dev, staging, prod."
  }
}

variable "name_prefix" {
  type        = string
  description = "Base resource prefix used in all generated names."

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{2,29}$", var.name_prefix))
    error_message = "name_prefix must match ^[a-z][a-z0-9-]{2,29}$ (lowercase, digits, hyphen; 3-30 chars)."
  }
}

variable "region_name" {
  type        = string
  description = "OpenStack region name."

  validation {
    condition     = trim(var.region_name) != ""
    error_message = "region_name is required and cannot be empty."
  }
}

variable "network_id" {
  type        = string
  description = "OpenStack network UUID for the instance NIC."

  validation {
    condition     = trim(var.network_id) != ""
    error_message = "network_id is required and cannot be empty."
  }
}

variable "subnet_id" {
  type        = string
  description = "OpenStack subnet UUID for the primary fixed IP."

  validation {
    condition     = trim(var.subnet_id) != ""
    error_message = "subnet_id is required and cannot be empty."
  }
}

variable "external_network_pool" {
  type        = string
  description = "External network pool/name used for floating IP allocation."
  default     = ""

  validation {
    condition     = (!var.assign_floating_ip || !var.create_floating_ip) || trim(var.external_network_pool) != ""
    error_message = "external_network_pool must be set when assign_floating_ip=true and create_floating_ip=true."
  }
}

variable "image_id" {
  type        = string
  description = "Image UUID used for the compute instance."

  validation {
    condition     = trim(var.image_id) != ""
    error_message = "image_id is required and cannot be empty."
  }
}

variable "flavor_name" {
  type        = string
  description = "OpenStack flavor name."

  validation {
    condition     = trim(var.flavor_name) != ""
    error_message = "flavor_name is required and cannot be empty."
  }
}

variable "key_pair_name" {
  type        = string
  description = "OpenStack key pair name for SSH access."

  validation {
    condition     = trim(var.key_pair_name) != ""
    error_message = "key_pair_name is required and cannot be empty."
  }
}

variable "availability_zone" {
  type        = string
  description = "Optional OpenStack availability zone."
  default     = null
}

variable "ssh_ingress_cidrs" {
  type        = list(string)
  description = "CIDRs allowed to connect over SSH (tcp/22)."

  validation {
    condition = length(var.ssh_ingress_cidrs) > 0 && alltrue([
      for cidr in var.ssh_ingress_cidrs : can(cidrhost(cidr, 0))
    ])
    error_message = "ssh_ingress_cidrs must contain at least one valid CIDR."
  }
}

variable "application_ingress_rules" {
  type = list(object({
    protocol         = string
    port_range_min   = number
    port_range_max   = number
    remote_ip_prefix = string
    description      = optional(string)
  }))
  description = "Additional ingress rules for application traffic."
  default     = []

  validation {
    condition = alltrue([
      for rule in var.application_ingress_rules :
      trim(rule.protocol) != "" &&
      rule.port_range_min > 0 &&
      rule.port_range_max >= rule.port_range_min &&
      can(cidrhost(rule.remote_ip_prefix, 0))
    ])
    error_message = "Each application ingress rule must have protocol, valid port range, and valid remote_ip_prefix CIDR."
  }
}

variable "allowed_egress_cidrs" {
  type        = list(string)
  description = "CIDRs allowed for outbound traffic."
  default     = ["0.0.0.0/0"]

  validation {
    condition = length(var.allowed_egress_cidrs) > 0 && alltrue([
      for cidr in var.allowed_egress_cidrs : can(cidrhost(cidr, 0))
    ])
    error_message = "allowed_egress_cidrs must contain at least one valid CIDR."
  }
}

variable "create_persistent_volume" {
  type        = bool
  description = "Create and attach a persistent block storage volume."
  default     = true
}

variable "persistent_volume_size_gb" {
  type        = number
  description = "Persistent block volume size in GB."
  default     = 80

  validation {
    condition     = var.persistent_volume_size_gb >= 10
    error_message = "persistent_volume_size_gb must be at least 10 GB."
  }
}

variable "persistent_volume_type" {
  type        = string
  description = "Optional OpenStack volume type."
  default     = null
}

variable "volume_device" {
  type        = string
  description = "Linux device path used for volume attachment."
  default     = "/dev/vdb"

  validation {
    condition     = can(regex("^/dev/[a-z]+[a-z0-9]*$", var.volume_device))
    error_message = "volume_device must look like a Linux device path (for example /dev/vdb)."
  }
}

variable "assign_floating_ip" {
  type        = bool
  description = "Whether to associate a floating IP to the instance."
  default     = true
}

variable "create_floating_ip" {
  type        = bool
  description = "When assigning a floating IP, create a new address from external_network_pool."
  default     = true
}

variable "existing_floating_ip_address" {
  type        = string
  description = "Existing floating IP address to associate when create_floating_ip=false."
  default     = null

  validation {
    condition     = (!var.assign_floating_ip || var.create_floating_ip) || (var.existing_floating_ip_address != null && trim(var.existing_floating_ip_address) != "")
    error_message = "existing_floating_ip_address is required when assign_floating_ip=true and create_floating_ip=false."
  }
}

variable "metadata" {
  type        = map(string)
  description = "Metadata applied to compute and volume resources."
  default     = {}
}

variable "user_data" {
  type        = string
  description = "Optional cloud-init user_data for bootstrap."
  default     = null
}
