"""
Gemini API を使った自動作詞・スタイルプロンプト生成の検証テスト
"""
import os
import json
from pathlib import Path
from pydantic import BaseModel, Field

# yt-analysis/.env から GEMINI_API_KEY を取得（ローカルテスト用フォールバック）
env_path = Path("/home/eiichi/src/yt-analysis/.env")
if env_path.exists():
    for line in env_path.read_text(encoding="utf-8").splitlines():
        if line.startswith("GEMINI_API_KEY="):
            os.environ["GEMINI_API_KEY"] = line.split("=", 1)[1].strip().strip('"').strip("'")
            break

api_key = os.environ.get("GEMINI_API_KEY")
print(f"GEMINI_API_KEY 取得状態: {'OK (設定あり)' if api_key else '未設定'}")

if not api_key:
    print("エラー: GEMINI_API_KEY が見つかりませんでした。")
    exit(1)

from google import genai
from google.genai import types

class SongGenerationPlan(BaseModel):
    title: str = Field(description="英語またはローマ字の短い楽曲タイトル（アンダースコア区切り、英数字のみ、例: autumn_rain）")
    style_prompt: str = Field(description="YuE2向けスタイルプロンプト（英語。ジャンル、BPM、楽器、ボーカルスタイル、ムードなど）")
    theme_description: str = Field(description="日本語による楽曲の着想・テーマ解説（1行程度）")
    lyrics: str = Field(description="日本語の歌詞。[Verse], [Chorus], [Outro] の構造を含める。")

client = genai.Client(api_key=api_key)

prompt = """
あなたはプロの作詞家兼音楽プロデューサーです。
AI音楽生成モデル「YuE2」に投入するための、楽曲の「タイトル」「スタイルプロンプト（英語）」「日本語歌詞」「テーマ解説」を生成してください。

【現在の条件】
- 季節: 秋（10月）
- 時間帯: 深夜（静寂・チル・内省的）
- 推奨ジャンル: Lo-fi hip hop, Acoustic Ballad, City Pop, Chillout のいずれか

【YuE2向け歌詞のルール】
- セクションタグ（[Verse], [Chorus], [Outro]）を含めること。
- [Verse] は日常や情景を描写し、[Chorus] は感情を高め、[Outro] で静かに余韻を残すこと。
- 各行はあまり長すぎず、日本語として美しく自然な言葉遣いにすること。
- 生成時間は1〜2分程度を想定するため、各セクション2〜4行程度でコンパクトにまとめること。

【スタイルプロンプトのルール】
- 英語で記述すること。
- 'Japanese, female vocal' や楽器（piano, acoustic guitar, lofi drum beats）、テンポ（例: 78 BPM）、ムード（nostalgic, mellow, relaxing）を具体的に指定すること。
"""

print("Gemini API で自動作詞・スタイル生成を実行中 (gemini-3.8-flash)...")
try:
    response = client.models.generate_content(
        model="gemini-3.8-flash",
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=SongGenerationPlan,
            temperature=0.7,
        ),
    )
    result = json.loads(response.text)
    print("\n=== [Gemini 生成結果] ===")
    print(f"タイトル: {result.get('title')}")
    print(f"テーマ: {result.get('theme_description')}")
    print(f"スタイル: {result.get('style_prompt')}")
    print("\n--- 歌詞 ---")
    print(result.get("lyrics"))
    print("=========================")
except Exception as e:
    print(f"Gemini API 呼び出しエラー: {e}")
