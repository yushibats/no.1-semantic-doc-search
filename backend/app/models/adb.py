"""
Autonomous Database 関連のモデル定義
"""
from typing import Optional
from pydantic import BaseModel


class ADBInfo(BaseModel):
    """Autonomous Database情報"""
    id: str
    display_name: str
    db_name: Optional[str] = None
    lifecycle_state: str
    cpu_core_count: Optional[int] = None
    data_storage_size_in_tbs: Optional[float] = None


class ADBGetRequest(BaseModel):
    """ADB取得リクエスト"""
    adb_name: str
    oci_compartment_ocid: str


class ADBGetResponse(BaseModel):
    """ADB取得レスポンス"""
    status: str
    message: str
    id: Optional[str] = None
    display_name: Optional[str] = None
    lifecycle_state: Optional[str] = None


class ADBOperationRequest(BaseModel):
    """ADB操作リクエスト（起動/停止）"""
    adb_ocid: str


class ADBOperationResponse(BaseModel):
    """ADB操作レスポンス"""
    status: str
    message: str
