output "id" {
  description = "Security group UUID."
  value       = openstack_networking_secgroup_v2.this.id
}

output "name" {
  description = "Security group name."
  value       = openstack_networking_secgroup_v2.this.name
}
