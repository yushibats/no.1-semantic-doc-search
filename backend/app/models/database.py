"""
データベース管理関連のデータモデル
"""
from typing import Optional, List, Dict, Any
from pydantic import BaseModel, Field


class DatabaseSettings(BaseModel):
    """データベース接続設定"""
    host: Optional[str] = None
    port: Optional[int] = 1521
    database: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None
    connection_type: str = "basic"  # basic, tns, wallet
    connection_string: Optional[str] = None


class DatabaseSettingsResponse(BaseModel):
    """データベース設定取得レスポンス"""
    settings: DatabaseSettings
    is_connected: bool = False
    status: str = "not_connected"


class DatabaseConnectionTestRequest(BaseModel):
    """データベース接続テストリクエスト"""
    settings: Optional[DatabaseSettings] = None


class DatabaseConnectionTestResponse(BaseModel):
    """データベース接続テストレスポンス"""
    success: bool
    message: str
    details: Optional[Dict[str, Any]] = None


class DatabaseInfo(BaseModel):
    """データベース情報"""
    version: Optional[str] = None
    instance_name: Optional[str] = None
    database_name: Optional[str] = None
    current_user: Optional[str] = None


class DatabaseInfoResponse(BaseModel):
    """データベース情報取得レスポンス"""
    success: bool
    info: Optional[DatabaseInfo] = None


class TableInfo(BaseModel):
    """テーブル情報"""
    table_name: str
    num_rows: Optional[int] = None
    created: Optional[str] = None
    last_analyzed: Optional[str] = None
    comments: Optional[str] = None


class DatabaseTablesResponse(BaseModel):
    """テーブル一覧取得レスポンス"""
    success: bool
    tables: List[TableInfo] = []
    total: int = 0
