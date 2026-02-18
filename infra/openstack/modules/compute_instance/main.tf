resource "openstack_networking_port_v2" "primary" {
  name               = "${var.name}-port"
  network_id         = var.network_id
  admin_state_up     = true
  security_group_ids = var.security_group_ids

  fixed_ip {
    subnet_id = var.subnet_id
  }
}

resource "openstack_compute_instance_v2" "this" {
  name              = var.name
  image_id          = var.image_id
  flavor_name       = var.flavor_name
  key_pair          = var.key_pair_name
  availability_zone = var.availability_zone
  metadata          = var.metadata
  user_data         = var.user_data

  network {
    port = openstack_networking_port_v2.primary.id
  }
}
