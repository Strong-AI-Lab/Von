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
    service_name       = "von"
    service_user       = var.bootstrap_service_user
    service_group      = var.bootstrap_service_group
    release_root       = var.bootstrap_release_root
    current_symlink    = var.bootstrap_current_symlink
    deploy_log_path    = var.bootstrap_deploy_log_path
    healthcheck_path   = var.bootstrap_healthcheck_path
    app_port           = var.bootstrap_app_port
    bootstrap_repo_url = var.bootstrap_repo_url
    bootstrap_repo_ref = var.bootstrap_repo_ref
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
    service_unit_content       = local.von_service_unit
    nginx_config_content       = local.von_nginx_config
    deploy_script_content      = local.von_deploy_script
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
