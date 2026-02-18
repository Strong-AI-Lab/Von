resource "openstack_networking_floatingip_v2" "this" {
  count = var.create ? 1 : 0

  pool = var.external_network_pool
}

locals {
  resolved_floating_ip_address = var.create ? openstack_networking_floatingip_v2.this[0].address : var.floating_ip_address
}

resource "openstack_networking_floatingip_associate_v2" "this" {
  floating_ip = local.resolved_floating_ip_address
  port_id     = var.port_id
}
