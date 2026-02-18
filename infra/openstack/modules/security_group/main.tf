resource "openstack_networking_secgroup_v2" "this" {
  name        = var.name
  description = var.description
}

locals {
  rules_by_index = {
    for index, rule in var.rules : tostring(index) => rule
  }
}

resource "openstack_networking_secgroup_rule_v2" "this" {
  for_each = local.rules_by_index

  security_group_id = openstack_networking_secgroup_v2.this.id
  direction         = lower(each.value.direction)
  ethertype         = try(each.value.ethertype, "IPv4")
  protocol          = try(each.value.protocol, null)
  port_range_min    = try(each.value.port_range_min, null)
  port_range_max    = try(each.value.port_range_max, null)
  remote_ip_prefix  = try(each.value.remote_ip_prefix, null)
  remote_group_id   = try(each.value.remote_group_id, null)
  description       = try(each.value.description, null)
}
