variable "name" {
  type        = string
  description = "Compute instance name."

  validation {
    condition     = trim(var.name) != ""
    error_message = "name is required and cannot be empty."
  }
}

variable "image_id" {
  type        = string
  description = "Image UUID for the compute instance."

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
  description = "OpenStack key pair name."

  validation {
    condition     = trim(var.key_pair_name) != ""
    error_message = "key_pair_name is required and cannot be empty."
  }
}

variable "network_id" {
  type        = string
  description = "Network UUID used by the primary port."

  validation {
    condition     = trim(var.network_id) != ""
    error_message = "network_id is required and cannot be empty."
  }
}

variable "subnet_id" {
  type        = string
  description = "Subnet UUID used for the primary fixed IP."

  validation {
    condition     = trim(var.subnet_id) != ""
    error_message = "subnet_id is required and cannot be empty."
  }
}

variable "security_group_ids" {
  type        = list(string)
  description = "Security group IDs applied to the primary port."

  validation {
    condition     = length(var.security_group_ids) > 0
    error_message = "security_group_ids must include at least one security group ID."
  }
}

variable "availability_zone" {
  type        = string
  description = "Optional availability zone."
  default     = null
}

variable "metadata" {
  type        = map(string)
  description = "Metadata attached to the instance."
  default     = {}
}

variable "user_data" {
  type        = string
  description = "Optional cloud-init user data."
  default     = null
}
