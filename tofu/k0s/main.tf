resource "terraform_data" "k0s" {
  triggers_replace = filesha256("${path.module}/k0sctl.yaml")

  provisioner "local-exec" {
    working_dir = path.module
    command     = "k0sctl apply --config k0sctl.yaml"
  }
}
