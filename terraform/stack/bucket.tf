# Object Storageバケットの作成
resource "oci_objectstorage_bucket" "document_storage_bucket" {
  compartment_id = var.compartment_ocid
  namespace      = data.oci_objectstorage_namespace.namespace.namespace
  name           = var.oci_bucket_name
  access_type    = "NoPublicAccess"

  # バケットのメタデータ
  metadata = {
    "purpose"     = "semantic-doc-search"
    "environment" = "production"
  }

  # バージョニングを有効化
  versioning = "Enabled"

  # 自動階層化を有効化（コスト最適化）
  auto_tiering = "InfrequentAccess"
}

# Object Storageのネームスペースを取得
data "oci_objectstorage_namespace" "namespace" {
  compartment_id = var.compartment_ocid
}
