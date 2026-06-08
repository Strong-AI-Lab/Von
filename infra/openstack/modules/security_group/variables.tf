variable "name" {
  type        = string
  description = "Security group name."

  validation {
    condition     = trimspace(var.name) != ""
    error_message = "name is required and cannot be empty."
  }
}

variable "description" {
  type        = string
  description = "Security group description."
  default     = "Managed by Terraform."
}

variable "rules" {
  type = list(object({
    direction        = string
    ethertype        = optional(string, "IPv4")
    protocol         = optional(string)
    port_range_min   = optional(number)
    port_range_max   = optional(number)
    remote_ip_prefix = optional(string)
    remote_group_id  = optional(string)
    description      = optional(string)
  }))
  description = "Security group ingress/egress rules."

  validation {
    condition = alltrue([
      for rule in var.rules : contains(["ingress", "egress"], lower(rule.direction))
    ])
    error_message = "Each security group rule direction must be ingress or egress."
  }

  validation {
    condition = alltrue([
      for rule in var.rules :
      (try(trimspace(rule.remote_ip_prefix), "") != "" || try(trimspace(rule.remote_group_id), "") != "")
    ])
    error_message = "Each rule must define remote_ip_prefix or remote_group_id."
  }

  validation {
    condition = alltrue([
      for rule in var.rules :
      try(trimspace(rule.remote_ip_prefix), "") == "" || can(cidrhost(rule.remote_ip_prefix, 0))
    ])
    error_message = "When provided, remote_ip_prefix must be a valid CIDR."
  }
}
