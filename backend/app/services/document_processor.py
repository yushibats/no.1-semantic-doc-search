import os
import logging
import uuid
from typing import List, Dict, Any, Optional
from pathlib import Path
import PyPDF2
from pptx import Presentation
from docx import Document as DocxDocument

logger = logging.getLogger(__name__)

class DocumentProcessor:
    """文書処理サービス - PDF/PPT/DOCX/TXTの解析"""
    
    def __init__(self):
        self.supported_formats = {
            'pdf': self._process_pdf,
            'pptx': self._process_pptx,
            'docx': self._process_docx,
            'txt': self._process_txt,
            'md': self._process_txt,
        }
    
    def process_document(self, file_path: str, filename: str) -> Dict[str, Any]:
        """
        文書を処理してテキストを抽出
        
        Args:
            file_path: ファイルパス
            filename: ファイル名
            
        Returns:
            処理結果(テキストチャンクとメタデータ)
        """
        try:
            # ファイル拡張子を取得
            file_ext = Path(filename).suffix.lower().lstrip('.')
            
            if file_ext not in self.supported_formats:
                raise ValueError(f"サポートされていないファイル形式: {file_ext}")
            
            # ファイル処理
            processor = self.supported_formats[file_ext]
            result = processor(file_path, filename)
            
            return {
                "success": True,
                "filename": filename,
                "file_type": file_ext,
                "chunks": result.get("chunks", []),
                "metadata": result.get("metadata", {}),
                "page_count": result.get("page_count", 0)
            }
            
        except Exception as e:
            logger.error(f"文書処理エラー: {filename} - {e}")
            return {
                "success": False,
                "error": str(e)
            }
    
    def _process_pdf(self, file_path: str, filename: str) -> Dict[str, Any]:
        """PDF処理"""
        chunks = []
        
        with open(file_path, 'rb') as file:
            reader = PyPDF2.PdfReader(file)
            page_count = len(reader.pages)
            
            for page_num, page in enumerate(reader.pages, start=1):
                text = page.extract_text()
                if text.strip():
                    # ページごとにチャンク化
                    chunks.append({
                        "page_number": page_num,
                        "text": text.strip(),
                        "chunk_id": f"{uuid.uuid4()}"
                    })
        
        return {
            "chunks": chunks,
            "page_count": page_count,
            "metadata": {
                "format": "PDF",
                "total_pages": page_count
            }
        }
    
    def _process_pptx(self, file_path: str, filename: str) -> Dict[str, Any]:
        """PowerPoint処理"""
        chunks = []
        prs = Presentation(file_path)
        slide_count = len(prs.slides)
        
        for slide_num, slide in enumerate(prs.slides, start=1):
            text_parts = []
            
            # スライド内の全テキストを抽出
            for shape in slide.shapes:
                if hasattr(shape, "text"):
                    text_parts.append(shape.text)
            
            slide_text = "\n".join(text_parts).strip()
            
            if slide_text:
                chunks.append({
                    "page_number": slide_num,
                    "text": slide_text,
                    "chunk_id": f"{uuid.uuid4()}"
                })
        
        return {
            "chunks": chunks,
            "page_count": slide_count,
            "metadata": {
                "format": "PowerPoint",
                "total_slides": slide_count
            }
        }
    
    def _process_docx(self, file_path: str, filename: str) -> Dict[str, Any]:
        """Word文書処理"""
        chunks = []
        doc = DocxDocument(file_path)
        
        # 段落ごとにテキストを抽出
        paragraphs_text = []
        for para in doc.paragraphs:
            if para.text.strip():
                paragraphs_text.append(para.text.strip())
        
        # 適切なサイズにチャンク化（1000文字程度）
        current_chunk = []
        current_length = 0
        chunk_size = 1000
        
        for para in paragraphs_text:
            para_length = len(para)
            
            if current_length + para_length > chunk_size and current_chunk:
                # 現在のチャンクを保存
                chunks.append({
                    "page_number": len(chunks) + 1,
                    "text": "\n\n".join(current_chunk),
                    "chunk_id": f"{uuid.uuid4()}"
                })
                current_chunk = [para]
                current_length = para_length
            else:
                current_chunk.append(para)
                current_length += para_length
        
        # 最後のチャンクを保存
        if current_chunk:
            chunks.append({
                "page_number": len(chunks) + 1,
                "text": "\n\n".join(current_chunk),
                "chunk_id": f"{uuid.uuid4()}"
            })
        
        return {
            "chunks": chunks,
            "page_count": len(chunks),
            "metadata": {
                "format": "Word",
                "total_paragraphs": len(paragraphs_text)
            }
        }
    
    def _process_txt(self, file_path: str, filename: str) -> Dict[str, Any]:
        """テキストファイル処理"""
        chunks = []
        
        with open(file_path, 'r', encoding='utf-8') as file:
            content = file.read()
        
        # 適切なサイズにチャンク化（1000文字程度）
        chunk_size = 1000
        text_chunks = []
        
        for i in range(0, len(content), chunk_size):
            chunk_text = content[i:i + chunk_size].strip()
            if chunk_text:
                text_chunks.append(chunk_text)
        
        for idx, chunk_text in enumerate(text_chunks, start=1):
            chunks.append({
                "page_number": idx,
                "text": chunk_text,
                "chunk_id": f"{uuid.uuid4()}"
            })
        
        return {
            "chunks": chunks,
            "page_count": len(chunks),
            "metadata": {
                "format": "Text",
                "total_chars": len(content)
            }
        }

# シングルトンインスタンス
document_processor = DocumentProcessor()
