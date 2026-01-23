from pydantic import BaseModel
from typing import Optional, List, Dict, Any
from datetime import datetime

class DocumentUploadResponse(BaseModel):
    """文書アップロードレスポンス"""
    success: bool
    message: str
    document_id: Optional[str] = None
    filename: Optional[str] = None
    file_size: Optional[int] = None
    content_type: Optional[str] = None
    uploaded_at: Optional[str] = None
    oci_path: Optional[str] = None

class DocumentInfo(BaseModel):
    """文書情報"""
    document_id: str
    filename: str
    file_size: int
    content_type: str
    uploaded_at: str
    oci_path: str
    page_count: Optional[int] = None
    status: str  # 'processing', 'completed', 'failed'

class DocumentListResponse(BaseModel):
    """文書リストレスポンス"""
    success: bool
    documents: List[DocumentInfo]
    total: int

class DocumentDeleteRequest(BaseModel):
    """文書削除リクエスト"""
    document_id: str

class DocumentDeleteResponse(BaseModel):
    """文書削除レスポンス"""
    success: bool
    message: str
