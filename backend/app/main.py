"""
セマンティック文書検索システム - メインAPIアプリケーション
"""
import logging
import sys
import os
import uuid
import time
import json
import secrets
from pathlib import Path
from typing import List, Optional, Dict, Any
from datetime import datetime, timedelta
from fastapi import FastAPI, HTTPException, UploadFile, File, Form, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from dotenv import load_dotenv

# ログ設定
LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s:%(lineno)d | %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

logging.basicConfig(
    level=logging.INFO,
    format=LOG_FORMAT,
    datefmt=LOG_DATE_FORMAT,
    handlers=[logging.StreamHandler(sys.stdout)]
)

logger = logging.getLogger(__name__)

# 環境変数の読み込み
project_root = Path(__file__).parent.parent
env_path = project_root / ".env"
if env_path.exists():
    load_dotenv(env_path)

# モデルのインポート
from app.models.oci import OCISettings, OCISettingsResponse, OCIConnectionTestRequest, OCIConnectionTestResponse
from app.models.document import DocumentUploadResponse, DocumentInfo, DocumentListResponse, DocumentDeleteRequest, DocumentDeleteResponse
from app.models.search import SearchQuery, SearchResponse
from app.models.database import (
    DatabaseSettings,
    DatabaseSettingsResponse,
    DatabaseConnectionTestRequest,
    DatabaseConnectionTestResponse,
    DatabaseInfo,
    DatabaseInfoResponse,
    TableInfo,
    DatabaseTablesResponse
)
from app.models.adb import (
    ADBGetRequest,
    ADBGetResponse,
    ADBOperationRequest,
    ADBOperationResponse
)

# サービスのインポート
from app.services.oci_service import oci_service
from app.services.document_processor import document_processor
from app.services.vector_search import vector_search_engine
from app.services.database_service import database_service
from app.services.adb_service import adb_service
from app.services.ai_copilot import get_copilot_service
from app.utils.auth_util import do_auth, get_username_from_connection_string

# FastAPIアプリケーション初期化
app = FastAPI(
    title="セマンティック文書検索システムAPI",
    version="0.1.0",
    description="OCI Object Storageベースのセマンティック文書検索システム"
)

# CORS設定
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# デバッグモード設定
debug_mode = os.getenv("DEBUG", "False").lower() == "true"

# セッション管理（メモリ内）
# Token -> {username: str, expires_at: datetime}
SESSIONS: Dict[str, Dict[str, Any]] = {}
SESSION_TIMEOUT_SECONDS = 86400  # 24時間

class LoginRequest(BaseModel):
    username: str
    password: str

# ストレージパス設定
STORAGE_PATH = Path(os.getenv("STORAGE_PATH", "./storage"))
UPLOAD_PATH = STORAGE_PATH / "uploads"
METADATA_PATH = STORAGE_PATH / "metadata"

# ディレクトリ作成
STORAGE_PATH.mkdir(parents=True, exist_ok=True)
UPLOAD_PATH.mkdir(parents=True, exist_ok=True)
METADATA_PATH.mkdir(parents=True, exist_ok=True)

# メタデータストレージ（本番環境ではDBを使用）
DOCUMENTS_METADATA_FILE = METADATA_PATH / "documents.json"

def load_documents_metadata() -> List[dict]:
    """文書メタデータを読み込む"""
    if DOCUMENTS_METADATA_FILE.exists():
        with open(DOCUMENTS_METADATA_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return []

def save_documents_metadata(documents: List[dict]):
    """文書メタデータを保存"""
    with open(DOCUMENTS_METADATA_FILE, 'w', encoding='utf-8') as f:
        json.dump(documents, f, ensure_ascii=False, indent=2)

# ========================================
# ヘルスチェック
# ========================================

@app.get("/")
async def root():
    """ルートエンドポイント"""
    return {
        "name": "Semantic Document Search API",
        "version": "0.1.0",
        "status": "running"
    }

@app.get("/api/health")
async def health_check():
    """ヘルスチェック"""
    return {
        "status": "healthy",
        "timestamp": datetime.now().isoformat()
    }

# ========================================
# 認証ミドルウェア
# ========================================

@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    """認証チェックミドルウェア"""
    # 除外パス
    if request.url.path in ["/api/login", "/api/logout", "/api/config", "/", "/api/health"] or \
       request.url.path.startswith("/public/") or \
       request.method == "OPTIONS":
        return await call_next(request)
        
    # デバッグモードは認証スキップ
    if debug_mode:
        return await call_next(request)
        
    # トークンチェック
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        # クエリパラメータもチェック
        token = request.query_params.get("token")
        if not token:
            return JSONResponse(status_code=401, content={"detail": "認証が必要です"})
    else:
        token = auth_header.split(" ")[1]
        
    if token not in SESSIONS:
        return JSONResponse(status_code=401, content={"detail": "無効または期限切れのトークンです"})
    
    # 有効期限チェック
    session_data = SESSIONS.get(token)
    if not session_data:
        return JSONResponse(status_code=401, content={"detail": "無効または期限切れのトークンです"})
        
    if session_data.get("expires_at") and session_data["expires_at"] < datetime.now():
        del SESSIONS[token]
        return JSONResponse(status_code=401, content={"detail": "セッションが期限切れです"})
    
    return await call_next(request)

# ========================================
# 認証エンドポイント
# ========================================

@app.get("/api/config")
def get_config():
    """フロントエンドに設定情報を公開"""
    return {
        "debug": debug_mode,
        "require_login": not debug_mode
    }

@app.post("/api/login")
def login(request: LoginRequest):
    """ログイン認証"""
    if debug_mode:
        # デバッグモード時は常に成功
        username = get_username_from_connection_string() or "debug-user"
        return {
            "status": "success", 
            "message": "デバッグモード: ログインをスキップしました", 
            "token": "debug-token", 
            "username": username
        }
    
    if do_auth(request.username, request.password):
        # トークン生成と保存
        token = secrets.token_hex(32)
        expires_at = datetime.now() + timedelta(seconds=SESSION_TIMEOUT_SECONDS)
        SESSIONS[token] = {
            "username": request.username,
            "expires_at": expires_at
        }
        
        # 期限切れトークンのクリーンアップ
        current_time = datetime.now()
        expired_tokens = [t for t, data in SESSIONS.items() if isinstance(data, dict) and data.get("expires_at") and data["expires_at"] < current_time]
        for t in expired_tokens:
            del SESSIONS[t]
            
        return {
            "status": "success", 
            "message": "ログインに成功しました", 
            "token": token, 
            "username": request.username
        }
    else:
        raise HTTPException(status_code=401, detail="ユーザー名またはパスワードが正しくありません")

@app.post("/api/logout")
def logout(request: Request):
    """ログアウト処理"""
    if debug_mode:
        return {"status": "success", "message": "デバッグモード: ログアウトをスキップしました"}

    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.startswith("Bearer "):
        token = auth_header.split(" ")[1]
        if token in SESSIONS:
            del SESSIONS[token]
            return {"status": "success", "message": "ログアウトしました"}
            
    return {"status": "success", "message": "既にログアウトしているか、無効なトークンです"}

# ========================================
# OCI設定管理
# ========================================

@app.get("/api/oci/settings", response_model=OCISettingsResponse)
async def get_oci_settings():
    """OCI設定を取得"""
    try:
        settings = oci_service.get_settings()
        
        # キー情報をマスク
        has_credentials = bool(settings.user_ocid and settings.tenancy_ocid and settings.fingerprint and settings.key_content)
        
        if settings.key_content:
            settings.key_content = '[CONFIGURED]'
        
        return OCISettingsResponse(
            settings=settings,
            is_configured=has_credentials,
            status="configured" if has_credentials else "not_configured",
            has_credentials=has_credentials
        )
    except Exception as e:
        logger.error(f"OCI設定取得エラー: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/oci/settings", response_model=OCISettingsResponse)
async def save_oci_settings(settings: OCISettings):
    """OCI設定を保存"""
    try:
        success = oci_service.save_settings(settings)
        
        if not success:
            raise HTTPException(status_code=500, detail="設定の保存に失敗しました")
        
        # 保存後の設定を取得
        saved_settings = oci_service.get_settings()
        
        # Private Keyの内容は返さない（セキュリティ上の理由）
        safe_settings = saved_settings.copy()
        if safe_settings.key_content:
            safe_settings.key_content = "[CONFIGURED]"
        
        return OCISettingsResponse(
            settings=safe_settings,
            is_configured=True,
            has_credentials=True,
            status="saved",
            message="✅ 設定を保存しました"
        )
            
    except Exception as e:
        logger.error(f"OCI設定保存エラー: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/oci/test", response_model=OCIConnectionTestResponse)
async def test_oci_connection(request: OCIConnectionTestRequest):
    """OCI接続テスト"""
    try:
        result = oci_service.test_connection(request.settings)
        
        return OCIConnectionTestResponse(
            success=result["success"],
            message=result["message"],
            details=result.get("details")
        )
    except Exception as e:
        logger.error(f"OCI接続テストエラー: {e}")
        return OCIConnectionTestResponse(
            success=False,
            message=f"接続テストエラー: {str(e)}"
        )

# ========================================
# 文書管理
# ========================================

@app.post("/api/documents/upload", response_model=DocumentUploadResponse)
async def upload_document(file: UploadFile = File(...)):
    """
    文書をアップロードして処理
    - ローカルに一時保存
    - OCI Object Storageにアップロード
    - テキスト抽出とベクトル化
    - インデックスに追加
    """
    try:
        # ファイルサイズチェック
        max_size = int(os.getenv("MAX_FILE_SIZE", 100000000))  # 100MB
        content = await file.read()
        
        if len(content) > max_size:
            raise HTTPException(status_code=400, detail=f"ファイルサイズが大きすぎます（最大{max_size}バイト）")
        
        # ファイル拡張子チェック
        allowed_extensions = os.getenv("ALLOWED_EXTENSIONS", "pdf,pptx,docx,txt,md").split(",")
        file_ext = Path(file.filename).suffix.lower().lstrip('.')
        
        if file_ext not in allowed_extensions:
            raise HTTPException(status_code=400, detail=f"サポートされていないファイル形式: {file_ext}")
        
        # 文書IDを生成
        document_id = str(uuid.uuid4())
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_filename = f"{timestamp}_{document_id[:8]}_{file.filename}"
        
        # ローカルに一時保存
        local_path = UPLOAD_PATH / safe_filename
        with open(local_path, 'wb') as f:
            f.write(content)
        
        # 文書処理（テキスト抽出）
        logger.info(f"文書を処理中: {file.filename}")
        processing_result = document_processor.process_document(str(local_path), file.filename)
        
        if not processing_result.get("success"):
            raise HTTPException(status_code=500, detail=f"文書処理エラー: {processing_result.get('error')}")
        
        chunks = processing_result.get("chunks", [])
        page_count = processing_result.get("page_count", 0)
        
        # ベクトルインデックスに追加
        logger.info(f"ベクトルインデックスに追加中: {file.filename}")
        vector_search_engine.add_document(document_id, file.filename, chunks)
        
        # メタデータを保存
        documents = load_documents_metadata()
        document_metadata = {
            "document_id": document_id,
            "filename": file.filename,
            "file_size": len(content),
            "content_type": file.content_type,
            "uploaded_at": datetime.now().isoformat(),
            "oci_path": f"uploads/{safe_filename}",
            "local_path": str(local_path),
            "page_count": page_count,
            "status": "completed",
            "chunk_count": len(chunks)
        }
        documents.append(document_metadata)
        save_documents_metadata(documents)
        
        logger.info(f"文書アップロード完了: {file.filename} (ID: {document_id})")
        
        return DocumentUploadResponse(
            success=True,
            message="文書のアップロードと処理が完了しました",
            document_id=document_id,
            filename=file.filename,
            file_size=len(content),
            content_type=file.content_type,
            uploaded_at=document_metadata["uploaded_at"],
            oci_path=document_metadata["oci_path"]
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"文書アップロードエラー: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/documents", response_model=DocumentListResponse)
async def list_documents():
    """文書リストを取得"""
    try:
        documents = load_documents_metadata()
        
        document_infos = [
            DocumentInfo(
                document_id=doc["document_id"],
                filename=doc["filename"],
                file_size=doc["file_size"],
                content_type=doc["content_type"],
                uploaded_at=doc["uploaded_at"],
                oci_path=doc["oci_path"],
                page_count=doc.get("page_count"),
                status=doc["status"]
            )
            for doc in documents
        ]
        
        return DocumentListResponse(
            success=True,
            documents=document_infos,
            total=len(document_infos)
        )
        
    except Exception as e:
        logger.error(f"文書リスト取得エラー: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.delete("/api/documents/{document_id}", response_model=DocumentDeleteResponse)
async def delete_document(document_id: str):
    """文書を削除"""
    try:
        documents = load_documents_metadata()
        
        # 対象文書を検索
        target_doc = None
        for doc in documents:
            if doc["document_id"] == document_id:
                target_doc = doc
                break
        
        if not target_doc:
            raise HTTPException(status_code=404, detail="文書が見つかりません")
        
        # ベクトルインデックスから削除
        vector_search_engine.delete_document(document_id)
        
        # ローカルファイルを削除
        local_path = Path(target_doc.get("local_path", ""))
        if local_path.exists():
            local_path.unlink()
        
        # メタデータから削除
        documents = [doc for doc in documents if doc["document_id"] != document_id]
        save_documents_metadata(documents)
        
        logger.info(f"文書を削除: {document_id}")
        
        return DocumentDeleteResponse(
            success=True,
            message="文書を削除しました"
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"文書削除エラー: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# ========================================
# セマンティック検索
# ========================================

@app.post("/api/search", response_model=SearchResponse)
async def search_documents(query: SearchQuery):
    """セマンティック検索を実行"""
    try:
        start_time = time.time()
        
        # 検索実行
        results = vector_search_engine.search(
            query=query.query,
            top_k=query.top_k,
            min_score=query.min_score,
            document_ids=query.document_ids
        )
        
        processing_time = time.time() - start_time
        
        logger.info(f"検索完了: クエリ='{query.query}', 結果数={len(results)}, 処理時間={processing_time:.3f}s")
        
        return SearchResponse(
            success=True,
            query=query.query,
            results=results,
            total_results=len(results),
            processing_time=processing_time
        )
        
    except Exception as e:
        logger.error(f"検索エラー: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# ========================================
# アプリケーション起動
# ========================================

# ========================================
# DB管理
# ========================================

@app.get("/api/db/settings", response_model=DatabaseSettingsResponse)
async def get_db_settings():
    """DB設定を取得"""
    try:
        settings_dict = database_service.get_settings()
        settings = DatabaseSettings(**settings_dict)
        
        is_connected = database_service.is_connected()
        
        return DatabaseSettingsResponse(
            settings=settings,
            is_connected=is_connected,
            status="connected" if is_connected else "not_connected"
        )
    except Exception as e:
        logger.error(f"DB設定取得エラー: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/db/settings")
async def save_db_settings(settings: DatabaseSettings):
    """DB設定を保存"""
    try:
        settings_dict = settings.model_dump()
        success = database_service.save_settings(settings_dict)
        
        if success:
            return {"success": True, "message": "DB設定を保存しました"}
        else:
            raise HTTPException(status_code=500, detail="設定の保存に失敗しました")
            
    except Exception as e:
        logger.error(f"DB設定保存エラー: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/db/test", response_model=DatabaseConnectionTestResponse)
async def test_db_connection(request: DatabaseConnectionTestRequest):
    """DB接続テスト"""
    try:
        settings_dict = None
        if request.settings:
            settings_dict = request.settings.model_dump()
        
        result = database_service.test_connection(settings_dict)
        
        return DatabaseConnectionTestResponse(
            success=result["success"],
            message=result["message"],
            details=result.get("details")
        )
    except Exception as e:
        logger.error(f"DB接続テストエラー: {e}")
        return DatabaseConnectionTestResponse(
            success=False,
            message=f"接続テストエラー: {str(e)}"
        )

@app.get("/api/db/info", response_model=DatabaseInfoResponse)
async def get_db_info():
    """データベース情報を取得"""
    try:
        info_dict = database_service.get_database_info()
        
        if info_dict:
            info = DatabaseInfo(**info_dict)
            return DatabaseInfoResponse(success=True, info=info)
        else:
            return DatabaseInfoResponse(success=False, info=None)
            
    except Exception as e:
        logger.error(f"データベース情報取得エラー: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/db/tables", response_model=DatabaseTablesResponse)
async def get_db_tables():
    """テーブル一覧を取得"""
    try:
        tables_list = database_service.get_tables()
        
        tables = [TableInfo(**table) for table in tables_list]
        
        return DatabaseTablesResponse(
            success=True,
            tables=tables,
            total=len(tables)
        )
            
    except Exception as e:
        logger.error(f"テーブル一覧取得エラー: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# ========================================
# Autonomous Database 管理
# ========================================

@app.post("/api/adb/get", response_model=ADBGetResponse)
async def get_adb_info(request: ADBGetRequest):
    """Autonomous Database情報を取得"""
    try:
        return adb_service.get_adb_info(request.adb_name, request.oci_compartment_ocid)
    except Exception as e:
        logger.error(f"ADB情報取得エラー: {e}")
        return ADBGetResponse(
            status="error",
            message=f"エラー: {str(e)}"
        )

@app.post("/api/adb/start", response_model=ADBOperationResponse)
async def start_adb(request: ADBOperationRequest):
    """Autonomous Databaseを起動"""
    try:
        return adb_service.start_adb(request.adb_ocid)
    except Exception as e:
        logger.error(f"ADB起動エラー: {e}")
        return ADBOperationResponse(
            status="error",
            message=f"起動エラー: {str(e)}"
        )

@app.post("/api/adb/stop", response_model=ADBOperationResponse)
async def stop_adb(request: ADBOperationRequest):
    """Autonomous Databaseを停止"""
    try:
        return adb_service.stop_adb(request.adb_ocid)
    except Exception as e:
        logger.error(f"ADB停止エラー: {e}")
        return ADBOperationResponse(
            status="error",
            message=f"停止エラー: {str(e)}"
        )

# ========================================
# AI Assistant エンドポイント
# ========================================

from fastapi.responses import StreamingResponse

class ChatMessage(BaseModel):
    """チャットメッセージ"""
    message: str
    context: Optional[dict] = None
    history: Optional[List[dict]] = None
    images: Optional[List[dict]] = None

@app.post("/api/copilot/chat")
async def copilot_chat_http(request: ChatMessage):
    """AI Assistant チャット（HTTP ストリーミング）"""
    copilot = get_copilot_service()
    
    async def generate():
        async for chunk in copilot.chat_stream(
            request.message,
            request.context,
            request.history,
            request.images
        ):
            # SSE形式でストリーミング
            yield f"data: {json.dumps({'content': chunk})}\n\n"
        yield "data: {\"done\": true}\n\n"
    
    return StreamingResponse(
        generate(),
        media_type="text/event-stream"
    )

# ========================================
# アプリケーション起動
# ========================================

if __name__ == "__main__":
    import uvicorn
    
    port = int(os.getenv("API_PORT", "8081"))
    host = os.getenv("API_HOST", "0.0.0.0")
    
    logger.info(f"サーバーを起動中: {host}:{port}")
    uvicorn.run(app, host=host, port=port)
