import os
import logging
import pickle
import numpy as np
from typing import List, Dict, Any, Optional
from pathlib import Path
import faiss
from sentence_transformers import SentenceTransformer
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

# 環境変数の読み込み
load_dotenv()

class VectorSearchEngine:
    """セマンティック検索エンジン - FAISS + Sentence Transformers"""
    
    def __init__(self):
        self.embedding_model_name = os.getenv("EMBEDDING_MODEL", "intfloat/multilingual-e5-large")
        self.vector_dimension = int(os.getenv("VECTOR_DIMENSION", "1024"))
        self.index_path = Path(os.getenv("VECTOR_INDEX_PATH", "./storage/vector_index"))
        
        self.model = None
        self.faiss_index = None
        self.document_chunks = []  # チャンク情報を保存
        
        # インデックスディレクトリを作成
        self.index_path.mkdir(parents=True, exist_ok=True)
        
        # モデルの遅延ロード
        self._load_model()
    
    def _load_model(self):
        """埋め込みモデルをロード"""
        try:
            if self.model is None:
                logger.info(f"埋め込みモデルをロード中: {self.embedding_model_name}")
                self.model = SentenceTransformer(self.embedding_model_name)
                logger.info("モデルのロードが完了しました")
        except Exception as e:
            logger.error(f"モデルのロードエラー: {e}")
            raise
    
    def _get_embedding(self, text: str) -> np.ndarray:
        """テキストをベクトル化"""
        try:
            # E5モデルの場合はプレフィックスを追加
            if "e5" in self.embedding_model_name.lower():
                text = f"query: {text}"
            
            embedding = self.model.encode([text], normalize_embeddings=True)[0]
            return embedding
        except Exception as e:
            logger.error(f"埋め込み生成エラー: {e}")
            raise
    
    def add_document(self, document_id: str, filename: str, chunks: List[Dict[str, Any]]):
        """
        文書をインデックスに追加
        
        Args:
            document_id: 文書ID
            filename: ファイル名
            chunks: テキストチャンクリスト
        """
        try:
            # FAISSインデックスを初期化（存在しない場合）
            if self.faiss_index is None:
                self.faiss_index = faiss.IndexFlatIP(self.vector_dimension)  # 内積ベース
            
            # チャンクをベクトル化
            for chunk in chunks:
                chunk_text = chunk.get("text", "")
                if not chunk_text.strip():
                    continue
                
                # E5モデルの場合はプレフィックスを追加
                if "e5" in self.embedding_model_name.lower():
                    embedding_text = f"passage: {chunk_text}"
                else:
                    embedding_text = chunk_text
                
                embedding = self.model.encode([embedding_text], normalize_embeddings=True)[0]
                
                # FAISSインデックスに追加
                self.faiss_index.add(np.array([embedding], dtype=np.float32))
                
                # メタデータを保存
                chunk_info = {
                    "document_id": document_id,
                    "filename": filename,
                    "page_number": chunk.get("page_number"),
                    "chunk_id": chunk.get("chunk_id"),
                    "text": chunk_text,
                    "metadata": chunk.get("metadata", {})
                }
                self.document_chunks.append(chunk_info)
            
            # インデックスを保存
            self._save_index()
            
            logger.info(f"文書をインデックスに追加: {filename} ({len(chunks)} chunks)")
            
        except Exception as e:
            logger.error(f"文書追加エラー: {e}")
            raise
    
    def search(self, query: str, top_k: int = 10, min_score: float = 0.0, 
               document_ids: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        """
        セマンティック検索を実行
        
        Args:
            query: 検索クエリ
            top_k: 取得する結果数
            min_score: 最小スコア閾値
            document_ids: 特定の文書IDに絞り込み
            
        Returns:
            検索結果リスト
        """
        try:
            if self.faiss_index is None or len(self.document_chunks) == 0:
                return []
            
            # クエリをベクトル化
            query_embedding = self._get_embedding(query)
            
            # FAISS検索
            scores, indices = self.faiss_index.search(
                np.array([query_embedding], dtype=np.float32), 
                min(top_k * 2, len(self.document_chunks))  # 余裕を持って取得
            )
            
            # 結果を整形
            results = []
            for score, idx in zip(scores[0], indices[0]):
                if idx == -1:  # 無効なインデックス
                    continue
                
                chunk_info = self.document_chunks[idx]
                
                # 文書IDフィルタリング
                if document_ids and chunk_info["document_id"] not in document_ids:
                    continue
                
                # スコア閾値フィルタリング
                if score < min_score:
                    continue
                
                results.append({
                    "document_id": chunk_info["document_id"],
                    "filename": chunk_info["filename"],
                    "page_number": chunk_info["page_number"],
                    "chunk_text": chunk_info["text"],
                    "score": float(score),
                    "metadata": chunk_info.get("metadata", {})
                })
                
                if len(results) >= top_k:
                    break
            
            return results
            
        except Exception as e:
            logger.error(f"検索エラー: {e}")
            raise
    
    def delete_document(self, document_id: str):
        """
        文書をインデックスから削除
        
        Args:
            document_id: 削除する文書ID
        """
        try:
            # 該当する文書のチャンクを削除
            indices_to_remove = [
                i for i, chunk in enumerate(self.document_chunks) 
                if chunk["document_id"] == document_id
            ]
            
            if not indices_to_remove:
                logger.warning(f"削除対象の文書が見つかりません: {document_id}")
                return
            
            # チャンク情報を削除
            for idx in reversed(indices_to_remove):
                del self.document_chunks[idx]
            
            # FAISSインデックスを再構築
            self._rebuild_index()
            
            logger.info(f"文書を削除: {document_id}")
            
        except Exception as e:
            logger.error(f"文書削除エラー: {e}")
            raise
    
    def _rebuild_index(self):
        """FAISSインデックスを再構築"""
        try:
            if len(self.document_chunks) == 0:
                self.faiss_index = None
                self._save_index()
                return
            
            # 新しいインデックスを作成
            self.faiss_index = faiss.IndexFlatIP(self.vector_dimension)
            
            # 全チャンクを再度ベクトル化して追加
            for chunk_info in self.document_chunks:
                chunk_text = chunk_info["text"]
                
                # E5モデルの場合はプレフィックスを追加
                if "e5" in self.embedding_model_name.lower():
                    embedding_text = f"passage: {chunk_text}"
                else:
                    embedding_text = chunk_text
                
                embedding = self.model.encode([embedding_text], normalize_embeddings=True)[0]
                self.faiss_index.add(np.array([embedding], dtype=np.float32))
            
            # インデックスを保存
            self._save_index()
            
        except Exception as e:
            logger.error(f"インデックス再構築エラー: {e}")
            raise
    
    def _save_index(self):
        """インデックスとメタデータを保存"""
        try:
            # FAISSインデックスを保存
            if self.faiss_index is not None:
                faiss.write_index(self.faiss_index, str(self.index_path / "index.faiss"))
            
            # チャンク情報を保存
            with open(self.index_path / "chunks.pkl", 'wb') as f:
                pickle.dump(self.document_chunks, f)
            
            logger.info("インデックスを保存しました")
            
        except Exception as e:
            logger.error(f"インデックス保存エラー: {e}")
            raise
    
    def load_index(self):
        """保存されたインデックスとメタデータをロード"""
        try:
            index_file = self.index_path / "index.faiss"
            chunks_file = self.index_path / "chunks.pkl"
            
            if index_file.exists() and chunks_file.exists():
                # FAISSインデックスをロード
                self.faiss_index = faiss.read_index(str(index_file))
                
                # チャンク情報をロード
                with open(chunks_file, 'rb') as f:
                    self.document_chunks = pickle.load(f)
                
                logger.info(f"インデックスをロードしました: {len(self.document_chunks)} chunks")
            else:
                logger.info("既存のインデックスが見つかりません")
                
        except Exception as e:
            logger.error(f"インデックスロードエラー: {e}")
            raise

# シングルトンインスタンス
vector_search_engine = VectorSearchEngine()

# 起動時にインデックスをロード
try:
    vector_search_engine.load_index()
except Exception as e:
    logger.warning(f"インデックスのロードに失敗しました: {e}")
