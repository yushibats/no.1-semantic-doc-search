"""
AI Copilot サービス - OCI OpenAI を使用したデータ分析支援
"""
import os
import asyncio
import base64
import re
import logging
from pathlib import Path
from typing import List, Dict, Any, Optional, AsyncGenerator, Tuple
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError
from dotenv import load_dotenv, find_dotenv
import oci
import oci.retry
import oci.util
from oci_openai import AsyncOciOpenAI, OciUserPrincipalAuth

logger = logging.getLogger(__name__)

# プロジェクトルートの.envファイルを読み込み
project_root = Path(__file__).parent.parent.parent.parent
env_path = project_root / ".env"
if env_path.exists():
    load_dotenv(env_path)
    logger.info(f"環境変数を読み込みました: {env_path}")
else:
    # フォールバック: find_dotenvで検索
    dotenv_path = find_dotenv()
    if dotenv_path:
        load_dotenv(dotenv_path)
        logger.info(f"環境変数を読み込みました: {dotenv_path}")
    else:
        logger.warning(".envファイルが見つかりません。環境変数はシステムから読み込みます。")


class AICopilotService:
    """AI Copilot サービスクラス"""
    
    def __init__(self):
        """AI Copilot サービスの初期化"""
        self.region = os.environ.get("OCI_REGION", "us-chicago-1")
        self.compartment_id = os.environ.get("OCI_COMPARTMENT_OCID", "")
        self.model_name = os.environ.get("AI_MODEL_NAME", "xai.grok-code-fast-1")
        self.vision_model_name = os.environ.get("VISION_MODEL_NAME", "google.gemini-2.5-flash")
        self.oci_config_profile = os.environ.get("OCI_CONFIG_PROFILE", "DEFAULT")
        self.oci_config_file = os.environ.get("OCI_CONFIG_FILE", "~/.oci/config")
        self.web_access_enabled = os.environ.get("COPILOT_WEB_ACCESS", "1").lower() not in ("0", "false", "no")
        
        if not self.compartment_id:
            logger.warning("OCI_COMPARTMENT_OCID が設定されていません")
    
    async def chat_stream(
        self,
        message: str,
        context: Optional[Dict[str, Any]] = None,
        history: Optional[List[Dict[str, str]]] = None,
        images: Optional[List[Dict[str, Any]]] = None
    ) -> AsyncGenerator[str, None]:
        """
        AIとチャットしてストリーミングレスポンスを取得
            
        Args:
            message: ユーザーメッセージ
            context: コンテキスト情報(データセット情報など)
            history: 会話履歴
            images: 画像リスト(最大10枚)
                
        Yields:
            str: AIレスポンスのチャンク
        """
        client = None
        try:
            system_prompt = self._build_system_prompt(context)

            # 画像がある場合、または会話履歴に画像が含まれる場合はGeminiを使用
            has_images = bool(images)
            has_history_images = False
            if history:
                for msg in history:
                    if isinstance(msg, dict) and msg.get('images'):
                        has_history_images = True
                        break
            
            if has_images or has_history_images:
                # 画像数の検証とログ
                image_count = len(images) if images else 0
                if image_count > 10:
                    logger.warning(f"画像数が上限を超えています: {image_count}枚 (最大10枚)")
                    images = images[:10]
                    image_count = 10
                
                if has_images:
                    logger.info(f"画像付きリクエスト: {image_count}枚の画像を処理中")
                else:
                    logger.info(f"会話履歴に画像が含まれているため、Geminiモデルを使用します")
                
                combined_prompt = self._build_combined_prompt(system_prompt, history, message)
                
                # Gemini APIを非同期で呼び出してストリーミング出力
                async for chunk in self._oci_generate_text_with_images_streaming(combined_prompt, images):
                    yield chunk
                return

            client = AsyncOciOpenAI(
                service_endpoint=f"https://inference.generativeai.{self.region}.oci.oraclecloud.com",
                auth=OciUserPrincipalAuth(),
                compartment_id=self.compartment_id,
            )

            messages = [{"role": "system", "content": system_prompt}]
            if history:
                messages.extend(history)
            messages.append({"role": "user", "content": message})

            logger.info(f"AIモデル {self.model_name} にリクエスト送信")

            stream = await client.chat.completions.create(
                model=self.model_name,
                messages=messages,
                stream=True,
                max_tokens=2048,
                temperature=0.0
            )

            async for chunk in stream:
                if chunk.choices and len(chunk.choices) > 0:
                    delta = chunk.choices[0].delta
                    if delta.content:
                        yield delta.content

        except Exception as e:
            logger.error(f"AI Copilot エラー: {str(e)}")
            error_message = f"エラーが発生しました: {str(e)}"
            yield error_message

        finally:
            if client is not None:
                try:
                    await client.close()
                except Exception:
                    pass

    def _build_combined_prompt(
        self,
        system_prompt: str,
        history: Optional[List[Dict[str, str]]],
        message: str
    ) -> str:
        """システムプロンプト、会話履歴、ユーザーメッセージを結合"""
        lines: List[str] = [system_prompt.strip()]
        if history:
            lines.append("")
            lines.append("会話履歴:")
            for item in history:
                if not item:  # None or 空辞書をスキップ
                    continue
                role = item.get("role", "")
                content = item.get("content", "")
                if role and content:
                    lines.append(f"{role.upper()}: {content}")
        lines.append("")
        if message:
            lines.append(f"USER: {message}")
        return "\n".join(lines).strip()

    async def _oci_generate_text_with_images_streaming(self, prompt: str, images: List[Dict[str, Any]]) -> AsyncGenerator[str, None]:
        """
        Gemini APIを呼び出してストリーミング出力
        
        OCI Generative AI Inference APIのネイティブなストリーミング機能を使用。
        """
        # API呼び出しを別スレッドで実行
        async for chunk in self._oci_generate_text_with_images_native_streaming(prompt, images):
            yield chunk

    async def _oci_generate_text_with_images_native_streaming(self, prompt: str, images: List[Dict[str, Any]]) -> AsyncGenerator[str, None]:
        """
        OCI Generative AIのネイティブストリーミングを使用
        Qiita記事：for event in response.events(): yield json.loads(event.data)["text"]
        リアルタイムストリーミングのためキューを使用
        """
        import json
        import queue
        import threading
        
        logger.info("=== Gemini Streaming開始 ===")
        
        # リアルタイムでチャンクを受け渡すためのキュー
        chunk_queue = queue.Queue()
        
        def stream_worker():
            """Qiita記事のシンプルなアプローチでストリーミング"""
            try:
                events_gen = self._oci_generate_text_with_images_streaming_sync(prompt, images)
                chunk_count = 0
                
                for event in events_gen:
                    if not event or not hasattr(event, 'data'):
                        continue
                    
                    try:
                        event_obj = json.loads(event.data)
                        
                        # Cohere形式: {"text": "..."}
                        if 'text' in event_obj:
                            if event_obj['text']:
                                chunk_count += 1
                                chunk_queue.put(('chunk', event_obj['text']))
                        # Gemini形式: {"message": {"content": [{"type": "TEXT", "text": "..."}]}}
                        elif 'message' in event_obj:
                            content_list = event_obj.get('message', {}).get('content', [])
                            for item in content_list:
                                if item.get('type') == 'TEXT' and item.get('text'):
                                    chunk_count += 1
                                    chunk_queue.put(('chunk', item['text']))
                        # finishReason, usageはスキップ
                    except (json.JSONDecodeError, KeyError, TypeError):
                        continue
                
                logger.info(f"Streaming完了: {chunk_count}チャンク")
                chunk_queue.put(('done', None))
            except Exception as e:
                logger.error(f"Streamingエラー: {e}", exc_info=True)
                chunk_queue.put(('error', str(e)))
        
        # 別スレッドでストリーミング開始
        thread = threading.Thread(target=stream_worker, daemon=True)
        thread.start()
        
        # キューからチャンクを取得して即座にyield
        while True:
            try:
                msg_type, data = await asyncio.to_thread(chunk_queue.get, timeout=0.05)
                
                if msg_type == 'chunk':
                    yield data
                elif msg_type == 'done':
                    break
                elif msg_type == 'error':
                    yield f"\n\nエラー: {data}"
                    break
            except queue.Empty:
                await asyncio.sleep(0.001)
                continue

    def _oci_generate_text_with_images_streaming_sync(self, prompt: str, images: List[Dict[str, Any]]):
        """
        OCI Generative AIのストリーミングAPIを呼び出す（同期版）
        """
        logger.info("_oci_generate_text_with_images_streaming_sync開始")
        
        config = None
        signer = None
        try:
            config = oci.config.from_file(self.oci_config_file, self.oci_config_profile)
            logger.info("OCI config読み込み成功")
        except Exception as e:
            logger.warning(f"OCI config読み込み失敗: {e}")
            config = None

        if config is None:
            try:
                signer = oci.auth.signers.get_resource_principals_signer()
                logger.info("Resource Principal Signer取得成功")
            except Exception as e:
                logger.warning(f"Resource Principal Signer取得失敗: {e}")
                signer = None
            if signer is None:
                try:
                    signer = oci.auth.signers.InstancePrincipalsSecurityTokenSigner()
                    logger.info("Instance Principal Signer取得成功")
                except Exception as e:
                    logger.warning(f"Instance Principal Signer取得失敗: {e}")
                    signer = None

        endpoint = f"https://inference.generativeai.{self.region}.oci.oraclecloud.com"
        logger.info(f"エンドポイント: {endpoint}")
        
        if config is not None:
            client = oci.generative_ai_inference.GenerativeAiInferenceClient(
                config=config,
                service_endpoint=endpoint,
                retry_strategy=oci.retry.NoneRetryStrategy(),
                timeout=(10, 240),
            )
            logger.info("Client作成成功 (config使用)")
        else:
            client = oci.generative_ai_inference.GenerativeAiInferenceClient(
                config={},
                signer=signer,
                service_endpoint=endpoint,
                retry_strategy=oci.retry.NoneRetryStrategy(),
                timeout=(10, 240),
            )
            logger.info("Client作成成功 (signer使用)")

        contents: List[Any] = [oci.generative_ai_inference.models.TextContent(text=prompt)]
        valid_image_count = 0
        for idx, img in enumerate((images or [])[:10]):
            data_url = (img or {}).get("data_url") or (img or {}).get("dataUrl")
            if not data_url:
                logger.warning(f"画像 #{idx+1}: data_url が見つかりません")
                continue
            mime, b64_data = self._parse_data_url(data_url)
            if not b64_data:
                logger.warning(f"画像 #{idx+1}: base64データの解析に失敗")
                continue
            url_value = f"data:{mime or 'image/jpeg'};base64,{b64_data}"
            image_url = oci.generative_ai_inference.models.ImageUrl(url=url_value, detail="AUTO")
            contents.append(oci.generative_ai_inference.models.ImageContent(image_url=image_url))
            valid_image_count += 1
        
        logger.info(f"有効な画像: {valid_image_count}枚")

        # is_stream=Trueでストリーミングを有効化
        chat_request = oci.generative_ai_inference.models.GenericChatRequest(
            api_format=oci.generative_ai_inference.models.BaseChatRequest.API_FORMAT_GENERIC,
            messages=[oci.generative_ai_inference.models.Message(role="USER", content=contents)],
            max_tokens=8192,
            temperature=0.0,
            top_p=0.95,
            top_k=1,
            is_stream=True  # ストリーミングを有効化
        )
        logger.info("ChatRequest作成完了 (is_stream=True)")

        chat_detail = oci.generative_ai_inference.models.ChatDetails(
            compartment_id=self.compartment_id,
            serving_mode=oci.generative_ai_inference.models.OnDemandServingMode(model_id=self.vision_model_name),
            chat_request=chat_request,
        )
        logger.info(f"ChatDetail作成完了 (model={self.vision_model_name})")

        logger.info("client.chat()呼び出し開始")
        try:
            response = client.chat(chat_detail)
            logger.info(f"client.chat()レスポンス取得, type={type(response)}")
            logger.info(f"response.data type={type(response.data)}")
            
            # response.dataはSSEClientオブジェクト
            # events()メソッドでイベントを取得
            events_gen = response.data.events()
            logger.info(f"events()取得完了, type={type(events_gen)}")
            return events_gen
        except Exception as e:
            logger.error(f"client.chat()呼び出しエラー: {e}", exc_info=True)
            raise

    async def _oci_generate_text_with_images(self, prompt: str, images: List[Dict[str, Any]]) -> str:
        return await asyncio.to_thread(self._oci_generate_text_with_images_sync, prompt, images)

    def _oci_generate_text_with_images_sync(self, prompt: str, images: List[Dict[str, Any]]) -> str:
        config = None
        signer = None
        try:
            config = oci.config.from_file(self.oci_config_file, self.oci_config_profile)
        except Exception:
            config = None

        if config is None:
            try:
                signer = oci.auth.signers.get_resource_principals_signer()
            except Exception:
                signer = None
            if signer is None:
                try:
                    signer = oci.auth.signers.InstancePrincipalsSecurityTokenSigner()
                except Exception:
                    signer = None

        endpoint = f"https://inference.generativeai.{self.region}.oci.oraclecloud.com"
        if config is not None:
            client = oci.generative_ai_inference.GenerativeAiInferenceClient(
                config=config,
                service_endpoint=endpoint,
                retry_strategy=oci.retry.NoneRetryStrategy(),
                timeout=(10, 240),
            )
        else:
            client = oci.generative_ai_inference.GenerativeAiInferenceClient(
                config={},
                signer=signer,
                service_endpoint=endpoint,
                retry_strategy=oci.retry.NoneRetryStrategy(),
                timeout=(10, 240),
            )

        contents: List[Any] = [oci.generative_ai_inference.models.TextContent(text=prompt)]
        valid_image_count = 0
        for idx, img in enumerate((images or [])[:10]):
            data_url = (img or {}).get("data_url") or (img or {}).get("dataUrl")
            if not data_url:
                logger.warning(f"画像 #{idx+1}: data_url が見つかりません")
                continue
            mime, b64_data = self._parse_data_url(data_url)
            if not b64_data:
                logger.warning(f"画像 #{idx+1}: base64データの解析に失敗")
                continue
            url_value = f"data:{mime or 'image/jpeg'};base64,{b64_data}"
            image_url = oci.generative_ai_inference.models.ImageUrl(url=url_value, detail="AUTO")
            contents.append(oci.generative_ai_inference.models.ImageContent(image_url=image_url))
            valid_image_count += 1
        
        logger.info(f"有効な画像: {valid_image_count}枚")

        chat_request = oci.generative_ai_inference.models.GenericChatRequest(
            api_format=oci.generative_ai_inference.models.BaseChatRequest.API_FORMAT_GENERIC,
            messages=[oci.generative_ai_inference.models.Message(role="USER", content=contents)],
            max_tokens=8192,
            temperature=0.0,
            top_p=0.95,
            top_k=1,
        )

        chat_detail = oci.generative_ai_inference.models.ChatDetails(
            compartment_id=self.compartment_id,
            serving_mode=oci.generative_ai_inference.models.OnDemandServingMode(model_id=self.vision_model_name),
            chat_request=chat_request,
        )

        response = client.chat(chat_detail)
        data = oci.util.to_dict(getattr(response, "data", response))
        
        # レスポンス情報をログ出力
        finish_reason = None
        try:
            finish_reason = data.get("chat_response", {}).get("choices", [{}])[0].get("finish_reason")
            if finish_reason:
                logger.info(f"Gemini応答完了理由: {finish_reason}")
        except Exception:
            pass
        
        extracted_text = self._extract_oci_chat_text(data)
        logger.info(f"Gemini応答テキスト長: {len(extracted_text)}文字")
        
        return extracted_text

    def _parse_data_url(self, data_url: str) -> Tuple[Optional[str], Optional[str]]:
        """Data URL から MIME タイプと base64 データを抽出"""
        if not data_url:
            return None, None
        m = re.match(r"^data:(?P<mime>[^;]+);base64,(?P<data>.+)$", data_url.strip())
        if not m:
            logger.debug("Data URL の形式が不正です")
            return None, None
        mime = m.group("mime")
        b64_data = m.group("data").strip()
        # base64データの妥当性を検証
        try:
            base64.b64decode(b64_data, validate=True)
        except Exception as e:
            logger.warning(f"base64 デコードエラー: {str(e)}")
            return mime, None
        return mime, b64_data

    def _extract_oci_chat_text(self, data: Dict[str, Any]) -> str:
        try:
            content = (
                data.get("chat_response", {})
                .get("choices", [{}])[0]
                .get("message", {})
                .get("content", [])
            )
            for item in content:
                if (item or {}).get("type") == "TEXT" and (item or {}).get("text"):
                    return (item or {}).get("text", "").strip()
        except Exception:
            pass

        texts: List[str] = []
        stack = [data]
        while stack:
            cur = stack.pop()
            if isinstance(cur, dict):
                if cur.get("type") == "TEXT" and isinstance(cur.get("text"), str):
                    texts.append(cur["text"])
                for v in cur.values():
                    stack.append(v)
            elif isinstance(cur, list):
                for v in cur:
                    stack.append(v)
        return "".join(texts).strip()
    
    def _build_system_prompt(self, context: Optional[Dict[str, Any]] = None) -> str:
        """
        システムプロンプトを構築
        
        Args:
            context: コンテキスト情報
            
        Returns:
            str: システムプロンプト
        """
        # df_local と df_proxy の利用可能性を判定
        has_df_local = False
        has_df_proxy = False
        
        if context:
            # 通常のデータセットがある場合
            if "dataset_info" in context:
                dataset_id = context["dataset_info"].get("dataset_id", "")
                # dataset_idが存在すればdf_localが利用可能
                if dataset_id:
                    has_df_local = True
            
            # OML Proxy Objectが利用可能かどうか
            # current_oml_table が選択されている場合
            if "current_oml_table" in context and context.get("current_oml_table"):
                has_df_proxy = True
        
        # 両方利用可能な場合の特別なプロンプト
        if has_df_local and has_df_proxy:
            base_prompt = """あなたは機械学習とデータ分析のエキスパートアシスタントです。
ユーザーのデータ分析タスクを支援し、以下の点について専門的なアドバイスを提供してください。

日本語で回答し、Pythonコードの例を含めることができます。

【重要】現在の環境について:
- **通常のデータセット** (`df_local`変数) と **OMLテーブル** (`df_proxy`変数) の両方が利用可能です
- **ユーザーが明示的に`df_local`または`df_proxy`を言及した場合**:
  - ユーザーが指定した変数のみを使用したコードを生成してください
  - 例: ユーザーが「df_localの最初の5行を表示」と言った場合 → `df_local.head(5)` のみ
  - 例: ユーザーが「df_proxyの統計情報」と言った場合 → `df_proxy.describe()` のみ
- **ユーザーが変数を明示しなかった場合のみ**、2つの別々のコードブロックを生成:
  1. **`df_local`を使用したコードブロック** (pandas DataFrame)
  2. **`df_proxy`を使用したコードブロック** (OML Proxy Object)
- 2つのコードブロックは、変数名(`df_local` vs `df_proxy`)のみ異なり、その他のロジックは同一にしてください
- 現在選択中のモデルは既に `model` 変数として読み込まれています
- **import文は絶対に使用しないでください** (すべて既にインポート済み)

**`df_local` (pandas DataFrame) の使用方法:**
- pandasの全機能が利用可能: `.head()`, `.tail()`, `.describe()`, `.fillna()`, `.dropna()` など
- グラフ描画: `plt.figure(figsize=(10, 6), dpi=240)` でグラフを作成
- 新しいデータセット作成: `df_local = df_local.dropna()` など

**`df_proxy` (OML Proxy Object) の使用方法:**
- OML操作: `.head()`, `.tail()`, `.shape`, `.columns`, `.describe()`
- DataFrameへの変換: `df_local = df_proxy.pull()`
- Oracle DB上で高速処理可能
- OMLアルゴリズム: `oml.glm()`, `oml.dt()`, `oml.rf()`, `oml.svm()`, `oml.nb()`

**コード生成例:**

ユーザー質問: 「データの最初の5行を表示して」

回答:

**df_local を使用する場合:**
```python
df_local.head(5)
```

**df_proxy を使用する場合:**
```python
df_proxy.head(5)
```

---

ユーザー質問: 「df_localの欠損値を確認して」

回答:
```python
df_local.isnull().sum()
```

---

ユーザー質問: 「df_proxyの統計情報を表示」

回答:
```python
df_proxy.describe()
```

---

**その他の重要な注意事項:**
- `plt.show()` は不要(自動キャプチャ)
- グラフの`dpi=240`を指定
- `numeric_only=True`で統計関数を使用
- 式として評価されるオブジェクトを返す (例: `df_local.head()`)
"""
        elif has_df_proxy:
            # df_proxy のみ利用可能
            base_prompt = """あなたは機械学習とデータ分析のエキスパートアシスタントです。
ユーザーのデータ分析タスクを支援し、Oracle Machine Learningの機能を活用した専門的なアドバイスを提供してください。

日本語で回答し、Pythonコードの例を含めることができます。

【重要】Pythonコード生成時の注意事項(Oracle Machine Learning環境):
- 現在選択中のOMLテーブルは `df_proxy` 変数(OML Proxy Object)として既に読み込まれています
- 現在選択中のモデルは既に `model` 変数として読み込まれています
- `oml` モジュール、`pd`(pandas)、`np`(numpy)、`plt`(matplotlib)は既にインポート済みです
- **import文は絶対に使用しないでください**

**OML Proxy Object(`df_proxy`)の主な機能:**
- `.head()`, `.tail()`, `.shape`, `.columns`: データ構造の確認
- `.pull()`: pandas DataFrameに変換 (例: `df_local = df_proxy.pull()`)
- `.describe()`: 統計情報の表示
- `.crosstab()`: クロス集計
- `.corr()`: 相関係数の計算

**Oracle Machine Learningアルゴリズムの利用:**
- `oml.glm()`: 一般化線形モデル
- `oml.dt()`: 決定木
- `oml.rf()`: ランダムフォレスト
- `oml.svm()`: サポートベクターマシン
- `oml.nb()`: ナイーブベイズ

**グラフ描画:**
- `plt.show()` は不要(自動キャプチャ)
- `dpi=240` を指定: `plt.figure(figsize=(10, 6), dpi=240)`

**pandas操作が必要な場合:**
- `.pull()` でDataFrameに変換: `df_local = df_proxy.pull()`
- 例: `df_local = df_proxy.pull(); df_local.fillna(df_local.mean(numeric_only=True))`

**データ分析のベストプラクティス:**
- OML Proxy ObjectはOracle DB上で処理されるため高速
- 結果は式で返す: `df_proxy.head(5)`, `df_proxy.describe()`
- モデル使用時: `model.predict(X)` (予測前に`.pull()`でDataFrame化が必要な場合あり)
"""
        elif has_df_local:
            # df_local のみ利用可能
            base_prompt = """あなたは機械学習とデータ分析のエキスパートアシスタントです。
ユーザーのデータ分析タスクを支援し、以下の点について専門的なアドバイスを提供してください：

1. データの前処理方法（欠損値処理、外れ値処理など）
2. 特徴量エンジニアリング（特徴量選択、変換、生成など）
3. 適切なモデルの選択
4. モデルのハイパーパラメータ調整
5. モデル評価指標の解釈

回答は簡潔で実用的なものにし、Pythonコードの例を含めることができます。
日本語で回答してください。

【重要】Pythonコードを生成する際の注意事項:
- 現在選択中のデータセットは既に `df_local` 変数(pandas DataFrame)として読み込まれています
- 現在選択中のモデルは既に `model` 変数として読み込まれています
- **import文は絶対に使用しないでください** (importは禁止されています)
- **変数名 `df` は使用禁止です。必ず `df_local` を使用してください**
- pandasはpd、numpyはnp、matplotlib.pyplotはplt、scikit-learnの各モジュールは既にインポート済みです
- `pd.read_csv()` などでファイルを読み込む必要はありません
- `joblib.load()` などでモデルを読み込む必要はありません
- コード内では直接 `df_local` や `model` を使用してください(例: `df_local[df_local['duration'].isnull()]`, `model.predict(X)`)
- `pd`、`np`、`plt` は既に利用可能ですので、importせずに直接使えます
  - 例: `plt.scatter(df_local.index, df_local['column'])` (※ `import matplotlib.pyplot as plt` は不要)
  - 例: `df_local['new_col'] = np.log(df_local['old_col'])` (※ `import numpy as np` は不要)
  - 例: `pd.DataFrame(...)` (※ `import pandas as pd` は不要)
- **グラフ描画に関する重要なルール:**
  - グラフを描画する場合は `plt.show()` を呼ぶ必要はありません(自動的にキャプチャされます)
  - グラフ描画後に `plt` だけを書く必要もありません。図は自動的に表示されます
  - **グラフの画質設定**: `plt.figure()` または `plt.subplots()` を使用する場合は、必ず `dpi=240` を指定してください
    - 例: `plt.figure(figsize=(10, 6), dpi=240)`
    - 例: `fig, ax = plt.subplots(figsize=(8, 6), dpi=240)`
  - `dpi=240` を指定することで、高解像度で鮮明なグラフが生成されます
- **新しいデータセット生成に関する重要なルール:**
  - ユーザーが「新しいデータセットを作成」「新しいデータセットを生成」「派生データセットを作成」などと明示的に指示した場合のみ、`df_local = ...` の形式でコードを生成してください
  - 例: ユーザーが「欠損値を削除して新しいデータセットを作成して」と言った場合 → `df_local = df_local.dropna()`
  - 例: ユーザーが「欠損値を確認して」と言った場合 → `df_local.isnull().sum()` (df_localを再代入しない)
  - 例: ユーザーが「年齢列を標準化して新しいデータセットにして」と言った場合 → `df_local['age'] = (df_local['age'] - df_local['age'].mean()) / df_local['age'].std()` の後に `df_local` を返す
  - データの分析・確認・可視化のみの場合は、`df_local` を再代入せず、結果を返すコードを生成してください
  - インプレース操作(`inplace=True`)も避け、ユーザーが明示的に新しいデータセット作成を指示した場合のみ使用してください
- **欠損値処理における重要な注意点:**
  - `df_local.mean()`, `df_local.fillna(df_local.mean())` などの統計関数を使用する場合は、必ず `numeric_only=True` パラメータを指定してください
  - 例: `df_local.fillna(df_local.mean(numeric_only=True), inplace=True)` または `df_mean = df_local.mean(numeric_only=True); df_local.fillna(df_mean)`
  - これにより、数値列のみを対象とし、非数値列（文字列、カテゴリなど）によるエラーを防ぎます
- **scikit-learn のモデルを使用する際の重要な注意点:**
  - 学習時にDataFrame(カラム名付き)を使用した場合、予測時もDataFrameを使用してください
  - 例: `model.fit(X, y)` で X が DataFrame なら、`model.predict(X_pred)` の X_pred も DataFrame にすること
  - NumPy配列を使う場合は、学習・予測の両方で NumPy 配列を使用してください
  - 良い例: `X_pred = pd.DataFrame(np.linspace(...).reshape(-1, 1), columns=['feature_name'])`
  - 悪い例: 学習時 DataFrame、予測時 NumPy 配列(特徴量名の警告が出ます)
- 実行結果を見やすくするために、`print()` よりも **式として評価されるオブジェクト** を返すコードを優先してください
  - 例: `df_local.head(5)`, `df_local[['col1','col2']].describe()`, `df_local['col'].value_counts()` など
  - モデル関連: `model.feature_importances_`, `model.get_params()`, `pd.Series(model.coef_, index=feature_names)` など
  - 最後の行が式の場合、その値(DataFrame や Series など)がテーブル形式で表示されます
- 行数が多い場合は、`df_local.head()`, `df_local.sample()`, `value_counts().head()` などを使い、不要に大量の行を出力しないようにしてください
- 列名の一覧や簡単な統計量を確認したい場合も、`list(df_local.columns)` を `print` するより `df_local.columns`, `df_local.describe()` などの式で返すようにしてください
- データを比較・フィルターするコードを生成する場合は、各カラムのデータ型に応じて適切な比較方法を選択してください(数値型では特に浮動小数点に対して `==` ではなく `np.isclose` や許容誤差を考慮した範囲比較を優先し、文字列型やカテゴリ型では `==` による厳密一致を用いてください)
"""
        else:
            # どちらも利用不可
            base_prompt = """あなたは機械学習とデータ分析のエキスパートアシスタントです。
ユーザーのデータ分析タスクを支援し、専門的なアドバイスを提供してください。

日本語で回答してください。

【注意】現在、データセットもOMLテーブルも選択されていません。
- 一般的なデータ分析の質問には回答できます
- Pythonコードを実行するには、まずデータセットまたはOMLテーブルを選択してください
"""
        
        # コンテキスト情報を追加
        context_info = ""  # 初期化を最初に行う
        
        if context:
            # 通常のデータセット情報を追加
            if "dataset_info" in context:
                ds = context["dataset_info"]
                context_info += "\n\n現在のデータセット情報 (df_local):\n"
                context_info += f"- 行数: {ds.get('row_count', 'N/A')}\n"
                context_info += f"- 列数: {ds.get('column_count', 'N/A')}\n"
                context_info += f"- カラム: {', '.join(ds.get('columns', []))}\n"
                
                if "missing_count" in ds:
                    missing = ds["missing_count"]
                    missing_cols = [k for k, v in missing.items() if v > 0]
                    if missing_cols:
                        context_info += f"- 欠損値のあるカラム: {', '.join(missing_cols)}\n"
            
            # OML Proxy Objectの情報を追加
            if "oml_proxies" in context and context.get("oml_proxies"):
                oml_proxies = context["oml_proxies"]
                context_info += "\n\n利用可能なOML Proxy Objects:\n"
                for proxy in oml_proxies:
                    context_info += f"- {proxy.get('table_name', 'N/A')}\n"
                    context_info += f"  - 行数: {proxy.get('row_count', 'N/A')}\n"
                    context_info += f"  - 列数: {proxy.get('column_count', 'N/A')}\n"
            
            # 現在選択中のOMLテーブルの詳細情報
            if "current_oml_table" in context and context.get("current_oml_table"):
                oml_table = context["current_oml_table"]
                context_info += "\n\n現在選択中のOMLテーブル (df_proxy):\n"
                context_info += f"- テーブル名: {oml_table.get('table_name', 'N/A')}\n"
                context_info += f"- 行数: {oml_table.get('row_count', 'N/A')}\n"
                context_info += f"- 列数: {oml_table.get('column_count', 'N/A')}\n"
                if oml_table.get('columns'):
                    context_info += f"- カラム: {', '.join(oml_table.get('columns', []))}\n"
            
            # 前処理タブのカラム別の欠損と概要情報を追加
            if "column_missing_and_summary" in context:
                cms = context["column_missing_and_summary"]
                context_info += f"\nカラム別の欠損と概要:\n"
                context_info += f"- データセット行数: {cms.get('row_count', 'N/A')}\n"
                context_info += f"- データセット列数: {cms.get('column_count', 'N/A')}\n"
                context_info += "- 各カラムの詳細:\n"
                for col in cms.get('columns', []):
                    context_info += f"  - {col.get('name', 'N/A')}:\n"
                    context_info += f"    - データ型: {col.get('dtype', 'N/A')}\n"
                    context_info += f"    - 欠損値数: {col.get('missing_count', 'N/A')}\n"
                    context_info += f"    - 欠損率: {col.get('missing_rate', 0)*100:.1f}%\n"
                    if col.get('is_numeric', False):
                        context_info += f"    - 平均値: {col.get('mean', 'N/A'):.3f}\n"
                        context_info += f"    - 中央値: {col.get('median', 'N/A'):.3f}\n"
                        context_info += f"    - 標準偏差: {col.get('std', 'N/A'):.3f}\n"
                        context_info += f"    - 最小値: {col.get('min', 'N/A'):.3f}\n"
                        context_info += f"    - 最大値: {col.get('max', 'N/A'):.3f}\n"
            
            if "model_info" in context:
                model_info = context["model_info"]
                context_info += f"\n現在のモデル情報:\n"
                context_info += f"- モデル名: {model_info.get('name', 'N/A')}\n"
                context_info += f"- モデルタイプ: {model_info.get('model_type', 'N/A')}\n"
                context_info += f"- アルゴリズム: {model_info.get('algorithm', 'N/A')}\n"
                context_info += f"- 学習スコア: {model_info.get('train_score', 'N/A')}\n"
                context_info += f"- テストスコア: {model_info.get('test_score', 'N/A')}\n"
                
                if "hyperparameters" in model_info and model_info["hyperparameters"]:
                    context_info += f"- ハイパーパラメータ:\n"
                    for key, value in model_info["hyperparameters"].items():
                        context_info += f"  - {key}: {value}\n"
                
                if "feature_names" in model_info and model_info["feature_names"]:
                    context_info += f"- 使用特徴量: {', '.join(model_info['feature_names'])}\n"
                
                if "target_column" in model_info:
                    context_info += f"- 目的変数: {model_info.get('target_column', 'N/A')}\n"
            
            if "trained_models" in context:
                trained_models = context["trained_models"] or []
                if trained_models:
                    context_info += "\n学習済みモデル一覧（画面に表示されているもの）:\n"
                    for m in trained_models:
                        context_info += f"- モデルID: {m.get('model_id', 'N/A')}\n"
                        context_info += f"  - モデルタイプ: {m.get('model_type', 'N/A')}\n"
                        context_info += f"  - 目標変数: {m.get('target_column', 'N/A')}\n"
                        train_score = m.get('train_score')
                        test_score = m.get('test_score')
                        if train_score is not None:
                            try:
                                context_info += f"  - 学習スコア: {float(train_score)*100:.2f}%\n"
                            except (TypeError, ValueError):
                                context_info += f"  - 学習スコア: {train_score}\n"
                        if test_score is not None:
                            try:
                                context_info += f"  - テストスコア: {float(test_score)*100:.2f}%\n"
                            except (TypeError, ValueError):
                                context_info += f"  - テストスコア: {test_score}\n"
                        created_at = m.get('created_at')
                        if created_at is not None:
                            context_info += f"  - 作成日時: {created_at}\n"
                        if m.get('is_current'):
                            context_info += "  - ※現在選択中のモデル\n"
            
            base_prompt += context_info        
        return base_prompt
    
    async def analyze_data(
        self,
        dataset_info: Dict[str, Any]
    ) -> str:
        """
        データセットを分析してレコメンデーションを提供
        
        Args:
            dataset_info: データセット情報
            
        Returns:
            str: 分析レポート
        """
        prompt = f"""以下のデータセット情報を分析して、適切な前処理手順とモデル選択のレコメンデーションを提供してください：

データセット情報:
- 行数: {dataset_info.get('row_count')}
- 列数: {dataset_info.get('column_count')}
- カラム: {', '.join(dataset_info.get('columns', []))}
- データタイプ: {dataset_info.get('dtypes')}
- 欠損値: {dataset_info.get('missing_count')}

以下の観点から分析してください：
1. データの品質評価
2. 推奨される前処理ステップ
3. 適切な特徴量エンジニアリング手法
4. 推奨されるモデルタイプ

【重要】コード例を提示する場合は、必ず変数名 `df_local` を使用してください（`df` は使用不可）。
"""
        
        response = ""
        async for chunk in self.chat_stream(prompt):
            response += chunk
        
        return response
    
    async def recommend_preprocessing(
        self,
        missing_info: Dict[str, int]
    ) -> str:
        """
        欠損値処理方法を推奨
        
        Args:
            missing_info: 欠損値情報
            
        Returns:
            str: 推奨事項
        """
        missing_cols = [k for k, v in missing_info.items() if v > 0]
        
        if not missing_cols:
            return "欠損値は検出されませんでした。"
        
        prompt = f"""以下のカラムに欠損値があります：
{chr(10).join([f'- {col}: {missing_info[col]}個の欠損値' for col in missing_cols])}

各カラムに対して最適な欠損値処理方法を推奨してください。
コード例を提示する場合は、必ず変数名 `df_local` を使用してください（`df` は使用不可）。
"""
        
        response = ""
        async for chunk in self.chat_stream(prompt):
            response += chunk
        
        return response


# グローバルインスタンス
_copilot_service = None


def get_copilot_service() -> AICopilotService:
    """AI Copilot サービスのシングルトンインスタンスを取得"""
    global _copilot_service
    if _copilot_service is None:
        _copilot_service = AICopilotService()
    return _copilot_service
