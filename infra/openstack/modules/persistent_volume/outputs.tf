output "volume_id" {
  description = "Persistent volume UUID."
  value       = openstack_blockstorage_volume_v3.this.id
}

output "volume_name" {
  description = "Persistent volume name."
  value       = openstack_blockstorage_volume_v3.this.name
}
