variable "create" {
  type        = bool
  description = "Create a new floating IP from external_network_pool."
  default     = true
}

variable "external_network_pool" {
  type        = string
  description = "Floating IP pool/external network name."
  default     = ""

  validation {
    condition     = !var.create || trimspace(var.external_network_pool) != ""
    error_message = "external_network_pool is required when create=true."
  }
}

variable "floating_ip_address" {
  type        = string
  description = "Existing floating IP address when create=false."
  default     = null

  validation {
    condition     = var.create || (var.floating_ip_address != null && trimspace(var.floating_ip_address) != "")
    error_message = "floating_ip_address is required when create=false."
  }
}

variable "port_id" {
  type        = string
  description = "Port UUID for floating IP association."

  validation {
    condition     = trimspace(var.port_id) != ""
    error_message = "port_id is required and cannot be empty."
  }
}
