variable "name" {
  type        = string
  description = "Volume name."

  validation {
    condition     = trimspace(var.name) != ""
    error_message = "name is required and cannot be empty."
  }
}

variable "description" {
  type        = string
  description = "Volume description."
  default     = "Von persistent data volume."
}

variable "size_gb" {
  type        = number
  description = "Volume size in GB."

  validation {
    condition     = var.size_gb >= 10
    error_message = "size_gb must be at least 10."
  }
}

variable "availability_zone" {
  type        = string
  description = "Optional availability zone."
  default     = null
}

variable "volume_type" {
  type        = string
  description = "Optional volume type."
  default     = null
}

variable "metadata" {
  type        = map(string)
  description = "Volume metadata."
  default     = {}
}
