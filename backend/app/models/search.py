from pydantic import BaseModel
from typing import Optional, List, Dict, Any

class SearchQuery(BaseModel):
    """検索クエリ"""
    query: str
    top_k: Optional[int] = 10
    min_score: Optional[float] = 0.0
    document_ids: Optional[List[str]] = None  # 特定の文書に絞り込む

class SearchResult(BaseModel):
    """検索結果"""
    document_id: str
    filename: str
    page_number: Optional[int] = None
    chunk_text: str
    score: float
    metadata: Optional[Dict[str, Any]] = None

class SearchResponse(BaseModel):
    """検索レスポンス"""
    success: bool
    query: str
    results: List[SearchResult]
    total_results: int
    processing_time: float  # 秒
