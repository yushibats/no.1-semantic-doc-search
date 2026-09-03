from __future__ import annotations

import json


INDEX_OUTPUT_CONTRACT = """出力形式:
- 次の技術的な形に厳密に一致するJSONオブジェクトを1つだけ返す
- 裏付けのない事実や不確かな事実は省略する
- 他のキーは追加しない
- room_area_estimatesは、図面上で部屋の境界と対応する寸法値を明確に確認できる場合だけ使用する
- room_area_estimatesのarea_m2とtatami_equivalentはサーバー側で再計算するため、0を返す
- 建物種別、構造、用途、面積、間取りを確認した場合は、facts.textに「建物種別: マンション」「構造: RC造」「専有面積: 80.4㎡」「現況間取り: 4DK」のようにラベル・値・単位・段階を同じ文で保持する
- 面積は専有面積、延床面積、建築面積、敷地面積、施工対象面積、部屋面積を区別し、種別が読めない場合は「面積（種別不明）」とする。面積種別や単位を推測しない
- ページテキストにある値は画像上の見出し・表・注記と照合し、ページ外の情報を補わない

{
  "summary": "ソースに基づく短い要約",
  "keywords": ["検索に使える語"],
  "facts": [{"text": "ソースに基づく事実", "source_locator": "page:N", "confidence": 0.0}],
  "room_area_estimates": [{
    "room_name": "LDK",
    "rectangles": [{"width_mm": 5200, "depth_mm": 4800, "operation": "ADD"}],
    "source_locator": "page:N",
    "evidence": "LDK内法の横寸法5200mmと縦寸法4800mmを図面上で確認",
    "confidence": 0.9,
    "area_m2": 0,
    "tatami_equivalent": 0,
    "conversion_m2_per_tatami": 1.62
  }]
}"""


def build_vlm_extraction_prompt(
    *,
    instruction: str,
    file_name: str,
    storage_object_name: str,
    page_number: int,
    page_text: str,
) -> str:
    """Build the same evidence envelope for production and the settings test UI."""
    context = {
        "original_file_name": file_name,
        "storage_object_name": storage_object_name,
        "page_number": page_number,
    }
    return (
        f"管理者の抽出指示:\n{instruction.strip()}\n\n"
        "入力コンテキスト:\n"
        f"{json.dumps(context, ensure_ascii=False)}\n"
        "- original_file_nameは利用者が付けた元ファイル名であり、資料種別の判定根拠にできます。\n"
        "- storage_object_nameは不変ID中心の技術的な保存キーです。名前や分類の判定根拠にしないでください。\n\n"
        f"ページテキスト:\n{str(page_text or '')[:12000]}\n\n"
        f"出典位置: page:{page_number}\n\n{INDEX_OUTPUT_CONTRACT}"
    )
