output "instance_id" {
  description = "Compute instance UUID."
  value       = openstack_compute_instance_v2.this.id
}

output "instance_name" {
  description = "Compute instance name."
  value       = openstack_compute_instance_v2.this.name
}

output "port_id" {
  description = "Primary instance port UUID."
  value       = openstack_networking_port_v2.primary.id
}

output "access_ip_v4" {
  description = "Access IPv4 value exposed by Nova."
  value       = openstack_compute_instance_v2.this.access_ip_v4
}
