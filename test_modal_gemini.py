"""
Gemini API 自律作詞のModal動作確認スクリプト (test_modal_gemini.py)
Modal Secret (gemini-secret) を使ってCPU単体で高速に作詞できるかを検証します。
"""
import modal
import os
import json
import datetime
from pydantic import BaseModel, Field

app = modal.App("test-modal-gemini")
image = modal.Image.debian_slim().pip_install("google-genai", "pydantic")

class SongPlan(BaseModel):
    title: str = Field(description="英語またはローマ字の短い楽曲タイトル（アンダースコア区切り、英数字のみ）")
    style_prompt: str = Field(description="YuE2向けスタイルプロンプト（英語）")
    theme_description: str = Field(description="日本語による楽曲の着想・テーマ解説")
    lyrics: str = Field(description="日本語の歌詞。[Verse], [Chorus], [Outro] の構造を含める。")

@app.function(image=image, secrets=[modal.Secret.from_name("gemini-secret")])
def test_gemini_lyricist():
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        return "⚠️ GEMINI_API_KEY が見つかりません。"

    from google import genai
    from google.genai import types

    client = genai.Client(api_key=api_key)
    prompt = """
AI音楽生成モデル「YuE2」に投入するための、秋の夜をテーマにした
「タイトル」「スタイルプロンプト（英語）」「日本語歌詞」「テーマ解説」を1曲分生成してください。
[Verse], [Chorus], [Outro] を含め、各2〜3行でコンパクトにしてください。
"""
    try:
        response = client.models.generate_content(
            model="gemini-3.8-flash",
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=SongPlan,
            ),
        )
        plan = json.loads(response.text)
        return f"🎉 成功！\n【タイトル】 {plan.get('title')}\n【テーマ】 {plan.get('theme_description')}\n【スタイル】 {plan.get('style_prompt')}\n\n【歌詞】\n{plan.get('lyrics')}"
    except Exception as e:
        return f"❌ エラー: {e}"

@app.local_entrypoint()
def main():
    print("Modal クラウド上で Gemini API の作詞テストを実行中...")
    result = test_gemini_lyricist.remote()
    print(result)
