resource "openstack_blockstorage_volume_v3" "this" {
  name              = var.name
  description       = var.description
  size              = var.size_gb
  availability_zone = var.availability_zone
  volume_type       = var.volume_type
  metadata          = var.metadata
}
