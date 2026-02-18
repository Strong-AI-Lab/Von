output "environment" {
  description = "Resolved environment label."
  value       = var.environment
}

output "instance_id" {
  description = "Compute instance UUID."
  value       = module.compute_instance.instance_id
}

output "instance_name" {
  description = "Compute instance name."
  value       = module.compute_instance.instance_name
}

output "instance_port_id" {
  description = "Primary instance port UUID."
  value       = module.compute_instance.port_id
}

output "security_group_id" {
  description = "Security group UUID."
  value       = module.security_group.id
}

output "persistent_volume_id" {
  description = "Persistent volume UUID when enabled."
  value       = var.create_persistent_volume ? module.persistent_volume[0].volume_id : null
}

output "floating_ip_address" {
  description = "Associated floating IP address when enabled."
  value       = var.assign_floating_ip ? module.floating_ip[0].floating_ip_address : null
}
