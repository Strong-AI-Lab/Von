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

variable "enable_managed_bootstrap" {
  type        = bool
  description = "When true, render and attach the managed Von cloud-init bootstrap if user_data is not explicitly provided."
  default     = true
}

variable "bootstrap_service_user" {
  type        = string
  description = "Linux service user used to run Von."
  default     = "von"

  validation {
    condition     = can(regex("^[a-z_][a-z0-9_-]*$", var.bootstrap_service_user))
    error_message = "bootstrap_service_user must be a valid Linux account name."
  }
}

variable "bootstrap_service_group" {
  type        = string
  description = "Linux service group used to run Von."
  default     = "von"

  validation {
    condition     = can(regex("^[a-z_][a-z0-9_-]*$", var.bootstrap_service_group))
    error_message = "bootstrap_service_group must be a valid Linux group name."
  }
}

variable "bootstrap_app_dir" {
  type        = string
  description = "Application root directory on the VM."
  default     = "/opt/von"

  validation {
    condition     = can(regex("^/", var.bootstrap_app_dir))
    error_message = "bootstrap_app_dir must be an absolute Linux path."
  }
}

variable "bootstrap_release_root" {
  type        = string
  description = "Directory containing versioned Von releases."
  default     = "/opt/von/releases"

  validation {
    condition     = can(regex("^/", var.bootstrap_release_root))
    error_message = "bootstrap_release_root must be an absolute Linux path."
  }
}

variable "bootstrap_current_symlink" {
  type        = string
  description = "Symlink path that points to the currently active release."
  default     = "/opt/von/current"

  validation {
    condition     = can(regex("^/", var.bootstrap_current_symlink))
    error_message = "bootstrap_current_symlink must be an absolute Linux path."
  }
}

variable "bootstrap_env_file" {
  type        = string
  description = "Path to environment file loaded by systemd service."
  default     = "/etc/von/von.env"

  validation {
    condition     = can(regex("^/", var.bootstrap_env_file))
    error_message = "bootstrap_env_file must be an absolute Linux path."
  }
}

variable "bootstrap_repo_url" {
  type        = string
  description = "Git repository URL used for initial bootstrap and subsequent host-side deployments."
  default     = "https://github.com/Strong-AI-Lab/Von.git"

  validation {
    condition     = trim(var.bootstrap_repo_url) != ""
    error_message = "bootstrap_repo_url cannot be empty."
  }
}

variable "bootstrap_repo_ref" {
  type        = string
  description = "Git ref (branch/tag/commit) used for bootstrap deployments."
  default     = "main"

  validation {
    condition     = trim(var.bootstrap_repo_ref) != ""
    error_message = "bootstrap_repo_ref cannot be empty."
  }
}

variable "bootstrap_python_package" {
  type        = string
  description = "Package name for Python runtime install on Debian/Ubuntu images."
  default     = "python3"

  validation {
    condition     = trim(var.bootstrap_python_package) != ""
    error_message = "bootstrap_python_package cannot be empty."
  }
}

variable "bootstrap_python_venv_package" {
  type        = string
  description = "Package name for Python venv tooling on Debian/Ubuntu images."
  default     = "python3-venv"

  validation {
    condition     = trim(var.bootstrap_python_venv_package) != ""
    error_message = "bootstrap_python_venv_package cannot be empty."
  }
}

variable "bootstrap_app_host" {
  type        = string
  description = "Host bind address used by the Von application service."
  default     = "127.0.0.1"

  validation {
    condition     = trim(var.bootstrap_app_host) != ""
    error_message = "bootstrap_app_host cannot be empty."
  }
}

variable "bootstrap_app_port" {
  type        = number
  description = "Port used by the Von application service behind the reverse proxy."
  default     = 5000

  validation {
    condition     = var.bootstrap_app_port > 0 && var.bootstrap_app_port < 65536
    error_message = "bootstrap_app_port must be between 1 and 65535."
  }
}

variable "bootstrap_domain_name" {
  type        = string
  description = "Server name configured in NGINX."
  default     = "localhost"

  validation {
    condition     = trim(var.bootstrap_domain_name) != ""
    error_message = "bootstrap_domain_name cannot be empty."
  }
}

variable "bootstrap_enable_https" {
  type        = bool
  description = "When true, configure NGINX for HTTPS termination with HTTP to HTTPS redirect."
  default     = true
}

variable "bootstrap_generate_self_signed_cert" {
  type        = bool
  description = "When true, generate a self-signed certificate if TLS files are missing."
  default     = true
}

variable "bootstrap_tls_cert_path" {
  type        = string
  description = "TLS certificate path consumed by NGINX."
  default     = "/etc/ssl/certs/von-selfsigned.crt"

  validation {
    condition     = can(regex("^/", var.bootstrap_tls_cert_path))
    error_message = "bootstrap_tls_cert_path must be an absolute Linux path."
  }
}

variable "bootstrap_tls_key_path" {
  type        = string
  description = "TLS private key path consumed by NGINX."
  default     = "/etc/ssl/private/von-selfsigned.key"

  validation {
    condition     = can(regex("^/", var.bootstrap_tls_key_path))
    error_message = "bootstrap_tls_key_path must be an absolute Linux path."
  }
}

variable "bootstrap_healthcheck_path" {
  type        = string
  description = "Application healthcheck path used by deploy validation."
  default     = "/health"

  validation {
    condition     = can(regex("^/", var.bootstrap_healthcheck_path))
    error_message = "bootstrap_healthcheck_path must start with '/'."
  }
}

variable "bootstrap_waitress_threads" {
  type        = number
  description = "VON_WAITRESS_THREADS value written to the managed environment file."
  default     = 16

  validation {
    condition     = var.bootstrap_waitress_threads >= 4 && var.bootstrap_waitress_threads <= 256
    error_message = "bootstrap_waitress_threads must be between 4 and 256."
  }
}

variable "bootstrap_flask_secret_key" {
  type        = string
  description = "Optional Flask secret key injected into runtime env for secure session signing."
  default     = null
  sensitive   = true

  validation {
    condition     = var.bootstrap_flask_secret_key == null || length(trim(var.bootstrap_flask_secret_key)) >= 32
    error_message = "bootstrap_flask_secret_key must be null or at least 32 characters."
  }
}

variable "bootstrap_google_oauth_strict_startup" {
  type        = bool
  description = "When true, Von startup fails-fast if Google OAuth settings are missing or unsafe."
  default     = false

  validation {
    condition     = !var.bootstrap_google_oauth_strict_startup || (var.bootstrap_google_oauth_redirect_uri != null && trim(var.bootstrap_google_oauth_redirect_uri) != "")
    error_message = "bootstrap_google_oauth_redirect_uri must be provided when bootstrap_google_oauth_strict_startup=true."
  }
}

variable "bootstrap_google_oauth_redirect_uri" {
  type        = string
  description = "Optional OAuth redirect URI override. Expected callback path: /von/api/auth/google/callback."
  default     = null

  validation {
    condition     = var.bootstrap_google_oauth_redirect_uri == null || can(regex("^https?://", trim(var.bootstrap_google_oauth_redirect_uri)))
    error_message = "bootstrap_google_oauth_redirect_uri must be null or an absolute http/https URI."
  }
}

variable "bootstrap_google_oauth_client_id" {
  type        = string
  description = "Optional inline OAuth client id for managed bootstrap env injection."
  default     = null
  sensitive   = true

  validation {
    condition     = var.bootstrap_google_oauth_client_id == null || trim(var.bootstrap_google_oauth_client_id) != ""
    error_message = "bootstrap_google_oauth_client_id must be null or non-empty."
  }
}

variable "bootstrap_google_oauth_client_secret" {
  type        = string
  description = "Optional inline OAuth client secret for managed bootstrap env injection."
  default     = null
  sensitive   = true

  validation {
    condition     = var.bootstrap_google_oauth_client_secret == null || trim(var.bootstrap_google_oauth_client_secret) != ""
    error_message = "bootstrap_google_oauth_client_secret must be null or non-empty."
  }

  validation {
    condition = (
      (var.bootstrap_google_oauth_client_id == null && var.bootstrap_google_oauth_client_secret == null) ||
      (var.bootstrap_google_oauth_client_id != null && var.bootstrap_google_oauth_client_secret != null)
    )
    error_message = "bootstrap_google_oauth_client_id and bootstrap_google_oauth_client_secret must be set together when using inline OAuth secrets."
  }
}

variable "bootstrap_google_oauth_client_id_file" {
  type        = string
  description = "File path used by runtime GOOGLE_OAUTH_CLIENT_ID_FILE when inline client id is not provided."
  default     = "/etc/von/secrets/google_oauth_client_id"

  validation {
    condition     = can(regex("^/", var.bootstrap_google_oauth_client_id_file))
    error_message = "bootstrap_google_oauth_client_id_file must be an absolute Linux path."
  }
}

variable "bootstrap_google_oauth_client_secret_file" {
  type        = string
  description = "File path used by runtime GOOGLE_OAUTH_CLIENT_SECRET_FILE when inline client secret is not provided."
  default     = "/etc/von/secrets/google_oauth_client_secret"

  validation {
    condition     = can(regex("^/", var.bootstrap_google_oauth_client_secret_file))
    error_message = "bootstrap_google_oauth_client_secret_file must be an absolute Linux path."
  }
}

variable "bootstrap_google_oauth_enable_dynamic_redirects" {
  type        = bool
  description = "Enable dynamic host-based redirect URI rewriting (intended for local/ngrok development only)."
  default     = false

  validation {
    condition     = !(var.bootstrap_google_oauth_enable_dynamic_redirects && var.bootstrap_google_oauth_strict_startup)
    error_message = "bootstrap_google_oauth_enable_dynamic_redirects cannot be true when bootstrap_google_oauth_strict_startup is enabled."
  }
}

variable "bootstrap_mongo_strict_startup" {
  type        = bool
  description = "When true, Von startup fails-fast if Mongo URI/security policy checks fail."
  default     = false

  validation {
    condition     = !(var.bootstrap_mongo_strict_startup && var.bootstrap_mongo_allow_local_fallback)
    error_message = "bootstrap_mongo_allow_local_fallback must be false when bootstrap_mongo_strict_startup=true."
  }

  validation {
    condition     = !var.bootstrap_mongo_strict_startup || var.bootstrap_mongo_require_tls
    error_message = "bootstrap_mongo_require_tls must be true when bootstrap_mongo_strict_startup=true."
  }
}

variable "bootstrap_mongo_startup_probe" {
  type        = bool
  description = "Enable Mongo auth/read/write startup probe at Von process startup."
  default     = false
}

variable "bootstrap_mongo_require_tls" {
  type        = bool
  description = "Require TLS-enabled MongoDB URI policy checks."
  default     = true
}

variable "bootstrap_mongo_allow_local_fallback" {
  type        = bool
  description = "Allow fallback from Atlas URI to local Mongo URI on connection failures."
  default     = true
}

variable "bootstrap_mongo_allowed_host_suffixes" {
  type        = list(string)
  description = "Allowed Mongo host suffixes for strict startup validation."
  default     = [".mongodb.net"]

  validation {
    condition     = length(var.bootstrap_mongo_allowed_host_suffixes) > 0
    error_message = "bootstrap_mongo_allowed_host_suffixes must contain at least one suffix."
  }
}

variable "bootstrap_mongo_uri" {
  type        = string
  description = "Optional inline Mongo URI for managed bootstrap secret-file injection."
  default     = null
  sensitive   = true

  validation {
    condition     = var.bootstrap_mongo_uri == null || can(regex("^mongodb(\\+srv)?://", trim(var.bootstrap_mongo_uri)))
    error_message = "bootstrap_mongo_uri must be null or a mongodb:// / mongodb+srv:// URI."
  }
}

variable "bootstrap_mongo_uri_file" {
  type        = string
  description = "Path used by runtime MONGO_URI_FILE."
  default     = "/etc/von/secrets/mongo_uri"

  validation {
    condition     = can(regex("^/", var.bootstrap_mongo_uri_file))
    error_message = "bootstrap_mongo_uri_file must be an absolute Linux path."
  }
}

variable "bootstrap_mongo_read_probe_collection" {
  type        = string
  description = "Collection name used for Mongo read-path startup probe."
  default     = "application_settings"

  validation {
    condition     = trim(var.bootstrap_mongo_read_probe_collection) != ""
    error_message = "bootstrap_mongo_read_probe_collection cannot be empty."
  }
}

variable "bootstrap_mongo_write_probe_collection" {
  type        = string
  description = "Collection name used for Mongo write-path startup probe."
  default     = "_von_startup_probe"

  validation {
    condition     = trim(var.bootstrap_mongo_write_probe_collection) != ""
    error_message = "bootstrap_mongo_write_probe_collection cannot be empty."
  }
}

variable "bootstrap_deploy_log_path" {
  type        = string
  description = "Deployment log file path used by /usr/local/bin/deploy_von_release.sh."
  default     = "/var/log/von/deploy.log"

  validation {
    condition     = can(regex("^/", var.bootstrap_deploy_log_path))
    error_message = "bootstrap_deploy_log_path must be an absolute Linux path."
  }
}

variable "bootstrap_deploy_audit_log_path" {
  type        = string
  description = "Audit log file path (JSONL) written by /usr/local/bin/deploy_von_release.sh."
  default     = "/var/log/von/deploy_audit.jsonl"

  validation {
    condition     = can(regex("^/", var.bootstrap_deploy_audit_log_path))
    error_message = "bootstrap_deploy_audit_log_path must be an absolute Linux path."
  }
}

variable "bootstrap_enable_monitoring" {
  type        = bool
  description = "Enable systemd-timer-driven operational monitoring checks."
  default     = true
}

variable "bootstrap_monitor_interval_minutes" {
  type        = number
  description = "Interval in minutes for operational monitoring checks."
  default     = 5

  validation {
    condition     = var.bootstrap_monitor_interval_minutes >= 1 && var.bootstrap_monitor_interval_minutes <= 1440
    error_message = "bootstrap_monitor_interval_minutes must be between 1 and 1440."
  }
}

variable "bootstrap_monitor_log_lookback_minutes" {
  type        = number
  description = "Journald lookback window in minutes for auth/DB failure detection."
  default     = 15

  validation {
    condition     = var.bootstrap_monitor_log_lookback_minutes >= 1 && var.bootstrap_monitor_log_lookback_minutes <= 1440
    error_message = "bootstrap_monitor_log_lookback_minutes must be between 1 and 1440."
  }
}

variable "bootstrap_monitor_auth_failure_threshold" {
  type        = number
  description = "Auth-failure event threshold before raising an alert."
  default     = 5

  validation {
    condition     = var.bootstrap_monitor_auth_failure_threshold >= 1 && var.bootstrap_monitor_auth_failure_threshold <= 10000
    error_message = "bootstrap_monitor_auth_failure_threshold must be between 1 and 10000."
  }
}

variable "bootstrap_monitor_db_failure_threshold" {
  type        = number
  description = "Database-failure event threshold before raising an alert."
  default     = 3

  validation {
    condition     = var.bootstrap_monitor_db_failure_threshold >= 1 && var.bootstrap_monitor_db_failure_threshold <= 10000
    error_message = "bootstrap_monitor_db_failure_threshold must be between 1 and 10000."
  }
}

variable "bootstrap_tls_expiry_warning_days" {
  type        = number
  description = "Raise an alert when TLS cert expires within this many days."
  default     = 21

  validation {
    condition     = var.bootstrap_tls_expiry_warning_days >= 1 && var.bootstrap_tls_expiry_warning_days <= 3650
    error_message = "bootstrap_tls_expiry_warning_days must be between 1 and 3650."
  }
}

variable "bootstrap_monitoring_events_log_path" {
  type        = string
  description = "JSONL log path for monitoring check events."
  default     = "/var/log/von/monitoring_events.jsonl"

  validation {
    condition     = can(regex("^/", var.bootstrap_monitoring_events_log_path))
    error_message = "bootstrap_monitoring_events_log_path must be an absolute Linux path."
  }
}

variable "bootstrap_enable_log_collection" {
  type        = bool
  description = "Enable periodic central log snapshot collection."
  default     = true
}

variable "bootstrap_log_collection_interval_minutes" {
  type        = number
  description = "Interval in minutes for central log snapshot collection."
  default     = 15

  validation {
    condition     = var.bootstrap_log_collection_interval_minutes >= 1 && var.bootstrap_log_collection_interval_minutes <= 1440
    error_message = "bootstrap_log_collection_interval_minutes must be between 1 and 1440."
  }
}

variable "bootstrap_log_collection_lookback_minutes" {
  type        = number
  description = "Lookback in minutes for journald export in central log collection."
  default     = 15

  validation {
    condition     = var.bootstrap_log_collection_lookback_minutes >= 1 && var.bootstrap_log_collection_lookback_minutes <= 1440
    error_message = "bootstrap_log_collection_lookback_minutes must be between 1 and 1440."
  }
}

variable "bootstrap_central_log_directory" {
  type        = string
  description = "Directory for consolidated app/systemd/reverse-proxy logs."
  default     = "/var/log/von/central"

  validation {
    condition     = can(regex("^/", var.bootstrap_central_log_directory))
    error_message = "bootstrap_central_log_directory must be an absolute Linux path."
  }
}

variable "bootstrap_central_log_audit_log_path" {
  type        = string
  description = "JSONL log path for central-log collection runs."
  default     = "/var/log/von/central_log_audit.jsonl"

  validation {
    condition     = can(regex("^/", var.bootstrap_central_log_audit_log_path))
    error_message = "bootstrap_central_log_audit_log_path must be an absolute Linux path."
  }
}

variable "bootstrap_enable_backup_automation" {
  type        = bool
  description = "Enable scheduled backup snapshots."
  default     = true
}

variable "bootstrap_backup_interval_hours" {
  type        = number
  description = "Interval in hours for backup snapshots."
  default     = 24

  validation {
    condition     = var.bootstrap_backup_interval_hours >= 1 && var.bootstrap_backup_interval_hours <= 720
    error_message = "bootstrap_backup_interval_hours must be between 1 and 720."
  }
}

variable "bootstrap_backup_retention_days" {
  type        = number
  description = "Retention period in days for backup artefacts."
  default     = 14

  validation {
    condition     = var.bootstrap_backup_retention_days >= 1 && var.bootstrap_backup_retention_days <= 3650
    error_message = "bootstrap_backup_retention_days must be between 1 and 3650."
  }
}

variable "bootstrap_backup_directory" {
  type        = string
  description = "Directory where backup archives are written."
  default     = "/var/backups/von"

  validation {
    condition     = can(regex("^/", var.bootstrap_backup_directory))
    error_message = "bootstrap_backup_directory must be an absolute Linux path."
  }
}

variable "bootstrap_backup_audit_log_path" {
  type        = string
  description = "JSONL log path for backup runs."
  default     = "/var/log/von/backup_audit.jsonl"

  validation {
    condition     = can(regex("^/", var.bootstrap_backup_audit_log_path))
    error_message = "bootstrap_backup_audit_log_path must be an absolute Linux path."
  }
}

variable "bootstrap_enable_restore_drill" {
  type        = bool
  description = "Enable scheduled restore-drill validation from latest backup."
  default     = true
}

variable "bootstrap_restore_drill_interval_days" {
  type        = number
  description = "Interval in days for automated restore drills."
  default     = 7

  validation {
    condition     = var.bootstrap_restore_drill_interval_days >= 1 && var.bootstrap_restore_drill_interval_days <= 3650
    error_message = "bootstrap_restore_drill_interval_days must be between 1 and 3650."
  }
}

variable "bootstrap_restore_drill_audit_log_path" {
  type        = string
  description = "JSONL log path for restore-drill runs."
  default     = "/var/log/von/restore_drill_audit.jsonl"

  validation {
    condition     = can(regex("^/", var.bootstrap_restore_drill_audit_log_path))
    error_message = "bootstrap_restore_drill_audit_log_path must be an absolute Linux path."
  }
}

variable "bootstrap_alert_webhook_url" {
  type        = string
  description = "Optional webhook URL for operational alerts. Leave null to disable external alert delivery."
  default     = null
}
