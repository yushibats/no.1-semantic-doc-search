import os
import json
import oci
import logging
import configparser
from pathlib import Path
from dotenv import load_dotenv, find_dotenv
from typing import Dict, Any, Optional
from app.models.oci import OCISettings

logger = logging.getLogger(__name__)

# .envファイルを読み込む
project_root = Path(__file__).parent.parent.parent
env_path = project_root / ".env"
if env_path.exists():
    load_dotenv(env_path)
elif find_dotenv():
    load_dotenv(find_dotenv())

# 環境変数から設定ファイルのパスを取得、デフォルトは ~/.oci/config
OCI_CONFIG_FILE = os.environ.get("OCI_CONFIG_FILE", os.path.expanduser("~/.oci/config"))
# キーファイルのパスは設定ファイルと同じディレクトリ内の oci_api_key.pem をデフォルトとする
OCI_KEY_FILE = os.environ.get("OCI_KEY_FILE", os.path.join(os.path.dirname(OCI_CONFIG_FILE), "oci_api_key.pem"))

class OCIService:
    def __init__(self):
        self.config_file = OCI_CONFIG_FILE
        self.key_file = OCI_KEY_FILE
        self._oci_config = None
        self._object_storage_client = None

    def get_settings(self) -> OCISettings:
        """保存された設定を読み込む"""
        if not os.path.exists(self.config_file) or not os.path.exists(self.key_file):
            return OCISettings()
        
        try:
            # Configファイルを読み込む
            config = configparser.ConfigParser()
            config.read(self.config_file)
            
            if 'DEFAULT' not in config:
                return OCISettings()
                
            defaults = config['DEFAULT']
            
            # Private Keyを読み込む
            # パーミッションを確認・修正
            if os.path.exists(self.key_file):
                os.chmod(self.key_file, 0o600)
                
            with open(self.key_file, 'r') as f:
                key_content = f.read()
            
            # Object Storage設定を環境変数から取得
            bucket_name = os.environ.get("OCI_BUCKET")
            namespace = os.environ.get("OCI_NAMESPACE")
                
            return OCISettings(
                user_ocid=defaults.get('user'),
                tenancy_ocid=defaults.get('tenancy'),
                fingerprint=defaults.get('fingerprint'),
                region=defaults.get('region'),
                key_content=key_content,
                bucket_name=bucket_name,
                namespace=namespace
            )
        except Exception as e:
            logger.error(f"設定ファイルの読み込みエラー: {e}")
            return OCISettings()

    def save_settings(self, settings: OCISettings) -> bool:
        """設定を保存する"""
        try:
            # ディレクトリを作成
            config_dir = os.path.dirname(self.config_file)
            os.makedirs(config_dir, exist_ok=True)
            os.chmod(config_dir, 0o700)
            
            # Private Keyの処理: 新規アップロードがない場合は既存のキーを保持
            if settings.key_content and settings.key_content != '[CONFIGURED]':
                # 新しいキーが提供された場合のみ保存
                with open(self.key_file, 'w') as f:
                    f.write(settings.key_content)
                os.chmod(self.key_file, 0o600)
            elif not os.path.exists(self.key_file):
                # キーファイルが存在せず、新しいキーも提供されていない場合はエラー
                logger.error("Private Keyが必要です")
                return False
            # else: 既存のキーファイルをそのまま使用
            
            # Configファイルを保存
            config = configparser.ConfigParser()
            config['DEFAULT'] = {
                'user': settings.user_ocid,
                'fingerprint': settings.fingerprint,
                'tenancy': settings.tenancy_ocid,
                'region': settings.region,
                'key_file': self.key_file
            }
            
            with open(self.config_file, 'w') as f:
                config.write(f)
            
            # Configファイルのパーミッションも600に設定
            os.chmod(self.config_file, 0o600)
                
            return True
        except Exception as e:
            logger.error(f"設定ファイルの保存エラー: {e}")
            return False

    def test_connection(self, settings: Optional[OCISettings] = None) -> Dict[str, Any]:
        """
        OCI接続テストを実行
        
        Args:
            settings: OCI設定(認証情報)
        """
        try:
            # 設定が渡されていない場合は保存済み設定を使用
            if settings is None:
                settings = self.get_settings()
            # キーが[CONFIGURED]の場合は既存のキーを読み込む
            elif settings.key_content == '[CONFIGURED]' and os.path.exists(self.key_file):
                with open(self.key_file, 'r') as f:
                    settings.key_content = f.read()
            
            # 必須フィールドのチェック
            if not all([settings.user_ocid, settings.tenancy_ocid, settings.fingerprint, settings.key_content, settings.region]):
                return {
                    "success": False,
                    "message": "認証情報が不完全です。User OCID, Tenancy OCID, Fingerprint, Region, Private Keyが必要です。"
                }
            
            # OCI設定を作成
            config = {
                "user": settings.user_ocid,
                "tenancy": settings.tenancy_ocid,
                "fingerprint": settings.fingerprint,
                "key_content": settings.key_content,
                "region": settings.region
            }
            
            # Identity Clientを使って接続テスト
            identity = oci.identity.IdentityClient(config)
            
            # ユーザー情報の取得を試行
            user = identity.get_user(config["user"]).data
            
            result = {
                "success": True,
                "message": "✅ 接続テストが成功しました",
                "details": {
                    "user_name": user.name,
                    "user_ocid": user.id,
                    "region": config["region"],
                    "tenancy": config["tenancy"]
                }
            }

            return result

        except Exception as e:
            logger.error(f"接続テストエラー: {e}")
            return {
                "success": False,
                "message": f"❌ 接続エラー: {str(e)}"
            }
    
    def get_oci_config(self) -> Dict[str, Any]:
        """OCI設定を取得"""
        if self._oci_config is None:
            settings = self.get_settings()
            if settings.key_content and settings.key_content != '[CONFIGURED]':
                self._oci_config = {
                    "user": settings.user_ocid,
                    "tenancy": settings.tenancy_ocid,
                    "fingerprint": settings.fingerprint,
                    "key_content": settings.key_content,
                    "region": settings.region
                }
        return self._oci_config
    
    def get_object_storage_client(self) -> oci.object_storage.ObjectStorageClient:
        """Object Storage Clientを取得"""
        if self._object_storage_client is None:
            config = self.get_oci_config()
            if config:
                self._object_storage_client = oci.object_storage.ObjectStorageClient(config)
        return self._object_storage_client

# シングルトンインスタンス
oci_service = OCIService()
