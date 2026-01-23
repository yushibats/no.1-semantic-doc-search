"""
データベース管理サービス
Oracle Database接続とクエリ実行を管理
"""
import logging
import json
import os
from pathlib import Path
from typing import Optional, Dict, Any, List
from datetime import datetime

logger = logging.getLogger(__name__)

# 設定ファイルパス
STORAGE_PATH = Path(os.getenv("STORAGE_PATH", "./storage"))
DB_SETTINGS_FILE = STORAGE_PATH / "metadata" / "db_settings.json"

# oracledbモジュールのインポート（オプション）
try:
    import oracledb
    ORACLEDB_AVAILABLE = True
except ImportError:
    logger.warning("oracledb モジュールが利用できません。pip install oracledb を実行してください。")
    ORACLEDB_AVAILABLE = False


class DatabaseService:
    """データベース管理サービス"""
    
    def __init__(self):
        """初期化"""
        self.connection = None
        self.settings = self._load_settings()
    
    def _load_settings(self) -> Dict[str, Any]:
        """DB設定をファイルから読み込む"""
        if DB_SETTINGS_FILE.exists():
            try:
                with open(DB_SETTINGS_FILE, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception as e:
                logger.error(f"DB設定読み込みエラー: {e}")
        
        return {
            "host": None,
            "port": 1521,
            "database": None,
            "username": None,
            "password": None,
            "connection_type": "basic",
            "connection_string": None
        }
    
    def _save_settings(self, settings: Dict[str, Any]):
        """DB設定をファイルに保存"""
        try:
            DB_SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(DB_SETTINGS_FILE, 'w', encoding='utf-8') as f:
                json.dump(settings, f, ensure_ascii=False, indent=2)
            logger.info("DB設定を保存しました")
        except Exception as e:
            logger.error(f"DB設定保存エラー: {e}")
            raise
    
    def get_settings(self) -> Dict[str, Any]:
        """現在の設定を取得"""
        # パスワードをマスク
        settings = self.settings.copy()
        if settings.get("password"):
            settings["password"] = "[CONFIGURED]"
        return settings
    
    def save_settings(self, settings: Dict[str, Any]) -> bool:
        """設定を保存"""
        try:
            # パスワードが[CONFIGURED]の場合は既存のものを保持
            if settings.get("password") == "[CONFIGURED]":
                settings["password"] = self.settings.get("password")
            
            self.settings = settings
            self._save_settings(settings)
            
            # 既存の接続をクローズ
            if self.connection:
                try:
                    self.connection.close()
                except:
                    pass
                self.connection = None
            
            return True
        except Exception as e:
            logger.error(f"設定保存エラー: {e}")
            return False
    
    def _create_connection(self, settings: Optional[Dict[str, Any]] = None):
        """データベース接続を作成"""
        if not ORACLEDB_AVAILABLE:
            raise Exception("oracledbモジュールが利用できません")
        
        if settings is None:
            settings = self.settings
        
        connection_type = settings.get("connection_type", "basic")
        
        if connection_type == "basic":
            # Basic接続（ホスト/ポート）
            dsn = oracledb.makedsn(
                host=settings["host"],
                port=settings["port"],
                service_name=settings["database"]
            )
            
            connection = oracledb.connect(
                user=settings["username"],
                password=settings["password"],
                dsn=dsn
            )
        
        elif connection_type == "tns":
            # TNS接続
            connection = oracledb.connect(
                user=settings["username"],
                password=settings["password"],
                dsn=settings["connection_string"]
            )
        
        elif connection_type == "wallet":
            # Wallet接続
            connection = oracledb.connect(
                user=settings["username"],
                password=settings["password"],
                dsn=settings["connection_string"],
                config_dir=settings.get("wallet_location")
            )
        
        else:
            raise ValueError(f"サポートされていない接続タイプ: {connection_type}")
        
        return connection
    
    def test_connection(self, settings: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """接続テストを実行"""
        try:
            if not ORACLEDB_AVAILABLE:
                return {
                    "success": False,
                    "message": "oracledbモジュールが利用できません。pip install oracledb を実行してください。"
                }
            
            connection = self._create_connection(settings)
            
            # テストクエリを実行
            cursor = connection.cursor()
            cursor.execute("SELECT 'OK' FROM DUAL")
            result = cursor.fetchone()
            cursor.close()
            connection.close()
            
            if result and result[0] == 'OK':
                return {
                    "success": True,
                    "message": "データベース接続に成功しました",
                    "details": {"status": "connected"}
                }
            else:
                return {
                    "success": False,
                    "message": "接続テストが失敗しました"
                }
        
        except Exception as e:
            logger.error(f"接続テストエラー: {e}")
            return {
                "success": False,
                "message": f"接続エラー: {str(e)}"
            }
    
    def get_database_info(self) -> Optional[Dict[str, Any]]:
        """データベース情報を取得"""
        try:
            if not ORACLEDB_AVAILABLE:
                return None
            
            connection = self._create_connection()
            cursor = connection.cursor()
            
            # データベースバージョン取得
            cursor.execute("SELECT * FROM v$version WHERE banner LIKE 'Oracle%'")
            version_row = cursor.fetchone()
            version = version_row[0] if version_row else None
            
            # インスタンス名取得
            cursor.execute("SELECT instance_name FROM v$instance")
            instance_row = cursor.fetchone()
            instance_name = instance_row[0] if instance_row else None
            
            # データベース名取得
            cursor.execute("SELECT name FROM v$database")
            db_row = cursor.fetchone()
            database_name = db_row[0] if db_row else None
            
            # 現在のユーザー取得
            cursor.execute("SELECT user FROM dual")
            user_row = cursor.fetchone()
            current_user = user_row[0] if user_row else None
            
            cursor.close()
            connection.close()
            
            return {
                "version": version,
                "instance_name": instance_name,
                "database_name": database_name,
                "current_user": current_user
            }
        
        except Exception as e:
            logger.error(f"データベース情報取得エラー: {e}")
            return None
    
    def get_tables(self) -> List[Dict[str, Any]]:
        """テーブル一覧を取得"""
        try:
            if not ORACLEDB_AVAILABLE:
                return []
            
            connection = self._create_connection()
            cursor = connection.cursor()
            
            # ユーザーのテーブル一覧を取得
            query = """
                SELECT 
                    t.table_name,
                    t.num_rows,
                    o.created,
                    t.last_analyzed,
                    c.comments
                FROM user_tables t
                LEFT JOIN user_objects o ON t.table_name = o.object_name AND o.object_type = 'TABLE'
                LEFT JOIN user_tab_comments c ON t.table_name = c.table_name
                ORDER BY t.table_name
            """
            
            cursor.execute(query)
            rows = cursor.fetchall()
            
            tables = []
            for row in rows:
                table_info = {
                    "table_name": row[0],
                    "num_rows": row[1],
                    "created": row[2].isoformat() if row[2] else None,
                    "last_analyzed": row[3].isoformat() if row[3] else None,
                    "comments": row[4]
                }
                tables.append(table_info)
            
            cursor.close()
            connection.close()
            
            return tables
        
        except Exception as e:
            logger.error(f"テーブル一覧取得エラー: {e}")
            return []
    
    def is_connected(self) -> bool:
        """接続状態を確認"""
        if not ORACLEDB_AVAILABLE:
            return False
        
        try:
            connection = self._create_connection()
            cursor = connection.cursor()
            cursor.execute("SELECT 1 FROM DUAL")
            cursor.fetchone()
            cursor.close()
            connection.close()
            return True
        except:
            return False


# グローバルインスタンス
database_service = DatabaseService()
