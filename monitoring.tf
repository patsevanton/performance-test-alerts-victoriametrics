locals {
  vmks_values = templatefile("${path.module}/vmks-values.yaml.tftpl", {
    ingress_public_ip = local.ingress_public_ip
  })
}

resource "local_file" "write_vmks_values" {
  content         = local.vmks_values
  filename        = "${path.module}/vmks-values.yaml"
  file_permission = "0644"
}
