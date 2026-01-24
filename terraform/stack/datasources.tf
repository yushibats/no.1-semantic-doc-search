data "template_file" "cloud_init_file" {
  template = file("./cloud_init/bootstrap.template.yaml")

  vars = {
    db_conn    = base64gzip("oml_user/${var.adb_password}@${lower(var.adb_name)}_high")
    wallet     = oci_database_autonomous_database_wallet.generated_autonomous_data_warehouse_wallet.content
    db_pass    = var.adb_password
    db_dsn     = "${lower(var.adb_name)}_high"
    comp_id    = var.compartment_ocid
    adb_name   = var.adb_name
    bucket     = var.oci_bucket_name
  }
}


data "template_cloudinit_config" "cloud_init" {
  gzip          = true
  base64_encode = true

  part {
    filename     = "bootstrap.yaml"
    content_type = "text/cloud-config"
    content      = data.template_file.cloud_init_file.rendered
  }
}
