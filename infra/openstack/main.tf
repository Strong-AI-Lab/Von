locals {
  base_name           = "${var.name_prefix}-${var.environment}"
  security_group_name = "${local.base_name}-sg"
  compute_name        = "${local.base_name}-vm"
  data_volume_name    = "${local.base_name}-data"

  common_metadata = merge({
    managed_by  = "terraform"
    project     = "von"
    environment = var.environment
  }, var.metadata)

  security_group_rules = concat(
    [
      for cidr in var.ssh_ingress_cidrs : {
        direction        = "ingress"
        ethertype        = "IPv4"
        protocol         = "tcp"
        port_range_min   = 22
        port_range_max   = 22
        remote_ip_prefix = cidr
        description      = "Allow SSH"
      }
    ],
    [
      for rule in var.application_ingress_rules : {
        direction        = "ingress"
        ethertype        = "IPv4"
        protocol         = rule.protocol
        port_range_min   = rule.port_range_min
        port_range_max   = rule.port_range_max
        remote_ip_prefix = rule.remote_ip_prefix
        description      = try(rule.description, null)
      }
    ],
    [
      for cidr in var.allowed_egress_cidrs : {
        direction        = "egress"
        ethertype        = "IPv4"
        protocol         = null
        port_range_min   = null
        port_range_max   = null
        remote_ip_prefix = cidr
        description      = "Allow outbound traffic"
      }
    ]
  )

  von_service_unit = templatefile("${path.module}/templates/systemd/von.service.tftpl", {
    service_user      = var.bootstrap_service_user
    service_group     = var.bootstrap_service_group
    working_directory = var.bootstrap_current_symlink
    env_file          = var.bootstrap_env_file
    app_host          = var.bootstrap_app_host
    app_port          = var.bootstrap_app_port
  })

  von_nginx_config = templatefile("${path.module}/templates/nginx/von.conf.tftpl", {
    domain_name      = var.bootstrap_domain_name
    app_host         = var.bootstrap_app_host
    app_port         = var.bootstrap_app_port
    enable_https     = var.bootstrap_enable_https
    tls_cert_path    = var.bootstrap_tls_cert_path
    tls_key_path     = var.bootstrap_tls_key_path
    healthcheck_path = var.bootstrap_healthcheck_path
  })

  von_deploy_script = templatefile("${path.module}/templates/scripts/deploy_von_release.sh.tftpl", {
    service_name           = "von"
    service_user           = var.bootstrap_service_user
    service_group          = var.bootstrap_service_group
    release_root           = var.bootstrap_release_root
    current_symlink        = var.bootstrap_current_symlink
    deploy_log_path        = var.bootstrap_deploy_log_path
    deploy_audit_log_path  = var.bootstrap_deploy_audit_log_path
    healthcheck_path       = var.bootstrap_healthcheck_path
    app_port               = var.bootstrap_app_port
    bootstrap_repo_url     = var.bootstrap_repo_url
    bootstrap_repo_ref     = var.bootstrap_repo_ref
  })

  von_ops_common_script = templatefile("${path.module}/templates/scripts/von_ops_common.sh.tftpl", {
    alert_webhook_url = var.bootstrap_alert_webhook_url != null ? var.bootstrap_alert_webhook_url : ""
  })

  von_monitor_script = templatefile("${path.module}/templates/scripts/von_monitor_health.sh.tftpl", {
    monitoring_events_log_path      = var.bootstrap_monitoring_events_log_path
    monitor_log_lookback_minutes    = var.bootstrap_monitor_log_lookback_minutes
    monitor_auth_failure_threshold  = var.bootstrap_monitor_auth_failure_threshold
    monitor_db_failure_threshold    = var.bootstrap_monitor_db_failure_threshold
    tls_expiry_warning_days         = var.bootstrap_tls_expiry_warning_days
    tls_cert_path                   = var.bootstrap_tls_cert_path
    enable_https                    = var.bootstrap_enable_https
    app_port                        = var.bootstrap_app_port
    healthcheck_path                = var.bootstrap_healthcheck_path
  })

  von_log_collector_script = templatefile("${path.module}/templates/scripts/von_collect_logs.sh.tftpl", {
    central_log_directory          = var.bootstrap_central_log_directory
    central_log_audit_log_path     = var.bootstrap_central_log_audit_log_path
    log_collection_lookback_minutes = var.bootstrap_log_collection_lookback_minutes
  })

  von_backup_script = templatefile("${path.module}/templates/scripts/von_backup_snapshot.sh.tftpl", {
    backup_directory      = var.bootstrap_backup_directory
    backup_retention_days = var.bootstrap_backup_retention_days
    backup_audit_log_path = var.bootstrap_backup_audit_log_path
    current_symlink       = var.bootstrap_current_symlink
    env_file              = var.bootstrap_env_file
  })

  von_restore_drill_script = templatefile("${path.module}/templates/scripts/von_restore_drill.sh.tftpl", {
    backup_directory            = var.bootstrap_backup_directory
    restore_drill_audit_log_path = var.bootstrap_restore_drill_audit_log_path
    release_root               = var.bootstrap_release_root
    env_file                   = var.bootstrap_env_file
  })

  von_monitor_service_unit = templatefile("${path.module}/templates/systemd/von-monitor.service.tftpl", {})
  von_monitor_timer_unit = templatefile("${path.module}/templates/systemd/von-monitor.timer.tftpl", {
    monitor_interval_minutes = var.bootstrap_monitor_interval_minutes
  })

  von_log_collector_service_unit = templatefile("${path.module}/templates/systemd/von-log-collector.service.tftpl", {})
  von_log_collector_timer_unit = templatefile("${path.module}/templates/systemd/von-log-collector.timer.tftpl", {
    log_collection_interval_minutes = var.bootstrap_log_collection_interval_minutes
  })

  von_backup_service_unit = templatefile("${path.module}/templates/systemd/von-backup.service.tftpl", {})
  von_backup_timer_unit = templatefile("${path.module}/templates/systemd/von-backup.timer.tftpl", {
    backup_interval_hours = var.bootstrap_backup_interval_hours
  })

  von_restore_drill_service_unit = templatefile("${path.module}/templates/systemd/von-restore-drill.service.tftpl", {})
  von_restore_drill_timer_unit = templatefile("${path.module}/templates/systemd/von-restore-drill.timer.tftpl", {
    restore_drill_interval_days = var.bootstrap_restore_drill_interval_days
  })

  managed_bootstrap_user_data = templatefile("${path.module}/templates/cloud-init/von_bootstrap.yaml.tftpl", {
    service_user               = var.bootstrap_service_user
    service_group              = var.bootstrap_service_group
    app_dir                    = var.bootstrap_app_dir
    release_root               = var.bootstrap_release_root
    current_symlink            = var.bootstrap_current_symlink
    current_symlink_parent     = dirname(var.bootstrap_current_symlink)
    env_file                   = var.bootstrap_env_file
    python_package             = var.bootstrap_python_package
    python_venv_package        = var.bootstrap_python_venv_package
    app_port                   = var.bootstrap_app_port
    app_host                   = var.bootstrap_app_host
    domain_name                = var.bootstrap_domain_name
    enable_https               = var.bootstrap_enable_https
    generate_self_signed_cert  = var.bootstrap_generate_self_signed_cert
    tls_cert_path              = var.bootstrap_tls_cert_path
    tls_key_path               = var.bootstrap_tls_key_path
    healthcheck_path           = var.bootstrap_healthcheck_path
    deploy_log_path            = var.bootstrap_deploy_log_path
    bootstrap_repo_url         = var.bootstrap_repo_url
    bootstrap_repo_ref         = var.bootstrap_repo_ref
    bootstrap_waitress_threads = var.bootstrap_waitress_threads
    flask_secret_key           = var.bootstrap_flask_secret_key
    google_oauth_strict_startup = var.bootstrap_google_oauth_strict_startup
    google_oauth_redirect_uri  = var.bootstrap_google_oauth_redirect_uri
    google_oauth_client_id     = var.bootstrap_google_oauth_client_id
    google_oauth_client_secret = var.bootstrap_google_oauth_client_secret
    google_oauth_client_id_file = var.bootstrap_google_oauth_client_id_file
    google_oauth_client_secret_file = var.bootstrap_google_oauth_client_secret_file
    google_oauth_enable_dynamic_redirects = var.bootstrap_google_oauth_enable_dynamic_redirects
    mongo_strict_startup       = var.bootstrap_mongo_strict_startup
    mongo_startup_probe        = var.bootstrap_mongo_startup_probe
    mongo_require_tls          = var.bootstrap_mongo_require_tls
    mongo_allow_local_fallback = var.bootstrap_mongo_allow_local_fallback
    mongo_allowed_host_suffixes = var.bootstrap_mongo_allowed_host_suffixes
    mongo_uri                  = var.bootstrap_mongo_uri
    mongo_uri_file             = var.bootstrap_mongo_uri_file
    mongo_read_probe_collection = var.bootstrap_mongo_read_probe_collection
    mongo_write_probe_collection = var.bootstrap_mongo_write_probe_collection
    enable_monitoring          = var.bootstrap_enable_monitoring
    enable_log_collection      = var.bootstrap_enable_log_collection
    enable_backup_automation   = var.bootstrap_enable_backup_automation
    enable_restore_drill       = var.bootstrap_enable_restore_drill
    backup_directory           = var.bootstrap_backup_directory
    central_log_directory      = var.bootstrap_central_log_directory
    service_unit_content       = local.von_service_unit
    nginx_config_content       = local.von_nginx_config
    deploy_script_content      = local.von_deploy_script
    ops_common_script_content  = local.von_ops_common_script
    monitor_script_content     = local.von_monitor_script
    log_collector_script_content = local.von_log_collector_script
    backup_script_content      = local.von_backup_script
    restore_drill_script_content = local.von_restore_drill_script
    monitor_service_content    = local.von_monitor_service_unit
    monitor_timer_content      = local.von_monitor_timer_unit
    log_collector_service_content = local.von_log_collector_service_unit
    log_collector_timer_content = local.von_log_collector_timer_unit
    backup_service_content     = local.von_backup_service_unit
    backup_timer_content       = local.von_backup_timer_unit
    restore_drill_service_content = local.von_restore_drill_service_unit
    restore_drill_timer_content = local.von_restore_drill_timer_unit
  })

  effective_user_data = (
    var.user_data != null && trimspace(var.user_data) != ""
  ) ? var.user_data : (
    var.enable_managed_bootstrap ? local.managed_bootstrap_user_data : null
  )
}

module "security_group" {
  source = "./modules/security_group"

  name        = local.security_group_name
  description = "Security group for Von ${var.environment} environment."
  rules       = local.security_group_rules
}

module "compute_instance" {
  source = "./modules/compute_instance"

  name               = local.compute_name
  image_id           = var.image_id
  flavor_name        = var.flavor_name
  key_pair_name      = var.key_pair_name
  network_id         = var.network_id
  subnet_id          = var.subnet_id
  security_group_ids = [module.security_group.id]
  availability_zone  = var.availability_zone
  metadata           = local.common_metadata
  user_data          = local.effective_user_data
}

module "persistent_volume" {
  count  = var.create_persistent_volume ? 1 : 0
  source = "./modules/persistent_volume"

  name              = local.data_volume_name
  size_gb           = var.persistent_volume_size_gb
  availability_zone = var.availability_zone
  volume_type       = var.persistent_volume_type
  metadata          = local.common_metadata
}

resource "openstack_compute_volume_attach_v2" "data_volume_attachment" {
  count = var.create_persistent_volume ? 1 : 0

  instance_id = module.compute_instance.instance_id
  volume_id   = module.persistent_volume[0].volume_id
  device      = var.volume_device
}

module "floating_ip" {
  count  = var.assign_floating_ip ? 1 : 0
  source = "./modules/floating_ip"

  create                = var.create_floating_ip
  external_network_pool = var.external_network_pool
  floating_ip_address   = var.existing_floating_ip_address
  port_id               = module.compute_instance.port_id
}
