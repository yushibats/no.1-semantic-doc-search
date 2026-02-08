"""
Autonomous Database 関連のデータモデル定義

Oracle Cloud Infrastructure (OCI) 上の Autonomous Database の
情報を表現するための Pydantic モデルを定義します。
各モデルは API リクエスト/レスポンスのデータ構造を表します。

主な機能:
- Autonomous Database の基本情報の表現
- ADB 操作（取得、起動、停止）のリクエスト/レスポンス定義
- OCI 固有の識別子と状態情報の管理
"""

from typing import Optional
from pydantic import BaseModel


class ADBInfo(BaseModel):
    """
    Autonomous Database の基本情報
    
    OCI 上の Autonomous Database インスタンスに関する
    基本的な情報を保持するデータモデルです。
    
    Attributes:
        id (str): Autonomous Database の OCID
        display_name (str): 表示名
        db_name (Optional[str]): データベース名
        lifecycle_state (str): ライフサイクル状態
        cpu_core_count (Optional[int]): CPU コア数
        data_storage_size_in_tbs (Optional[float]): ストレージ容量 (TB)
    """
    id: str
    display_name: str
    db_name: Optional[str] = None
    lifecycle_state: str
    cpu_core_count: Optional[int] = None
    data_storage_size_in_tbs: Optional[float] = None


class ADBGetRequest(BaseModel):
    """
    Autonomous Database 取得リクエスト
    
    特定の Autonomous Database を識別するために必要な
    情報を含むリクエストモデルです。
    
    Attributes:
        adb_name (str): Autonomous Database の名前
        oci_compartment_ocid (str): コンパートメントの OCID
    """
    adb_name: str
    oci_compartment_ocid: str


class ADBGetResponse(BaseModel):
    """
    Autonomous Database 取得レスポンス
    
    Autonomous Database の取得結果を返すレスポンスモデルです。
    
    Attributes:
        status (str): 処理結果のステータス
        message (str): 処理結果のメッセージ
        id (Optional[str]): Autonomous Database の OCID
        display_name (Optional[str]): 表示名
        lifecycle_state (Optional[str]): ライフサイクル状態
    """
    status: str
    message: str
    id: Optional[str] = None
    display_name: Optional[str] = None
    lifecycle_state: Optional[str] = None


class ADBOperationRequest(BaseModel):
    """
    Autonomous Database 操作リクエスト（起動/停止）
    
    Autonomous Database の起動または停止操作を行うために
    必要な情報を含むリクエストモデルです。
    
    Attributes:
        adb_ocid (str): 操作対象の Autonomous Database OCID
    """
    adb_ocid: str


class ADBOperationResponse(BaseModel):
    """
    Autonomous Database 操作レスポンス
    
    Autonomous Database の操作結果を返すレスポンスモデルです。
    
    Attributes:
        status (str): 処理結果のステータス
        message (str): 処理結果の詳細メッセージ
    """
    status: str
    message: str
