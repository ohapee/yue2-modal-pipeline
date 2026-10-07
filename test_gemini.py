"""
最新プロンプト（長尺2分58秒制限 & 多ジャンル対応）の検証スクリプト (test_gemini.py)
"""
import os
import json
import random
from pathlib import Path
from pydantic import BaseModel, Field

# yt-analysis/.env から GEMINI_API_KEY を取得（ローカルテスト用）
env_path = Path("/home/eiichi/src/yt-analysis/.env")
if env_path.exists():
    for line in env_path.read_text(encoding="utf-8").splitlines():
        if line.startswith("GEMINI_API_KEY="):
            os.environ["GEMINI_API_KEY"] = line.split("=", 1)[1].strip().strip('"').strip("'")
            break

api_key = os.environ.get("GEMINI_API_KEY")
if not api_key:
    print("エラー: GEMINI_API_KEY が見つかりませんでした。")
    exit(1)

from google import genai
from google.genai import types

class SongGenerationPlan(BaseModel):
    title: str = Field(description="英語またはローマ字の短い楽曲タイトル（アンダースコア区切り、英数字のみ、例: starlight_runner）")
    style_prompt: str = Field(description="YuE2向けスタイルプロンプト（英語。ジャンル、BPM、楽器、ボーカルスタイル、ムードなど）")
    theme_description: str = Field(description="日本語による楽曲の着想・テーマ解説（1行程度）")
    lyrics: str = Field(description="日本語の歌詞。[Intro], [Verse 1], [Pre-Chorus], [Chorus], [Verse 2], [Chorus], [Outro] の構造を含める。")

client = genai.Client(api_key=api_key)

genre_sample = "80s Japanese city pop, groovy slap bass, bright synth brass, funk guitar stabs, nostalgic Tokyo night, 116 BPM"
vocal_sample = "Japanese female vocal, stylish airy voice, modern idol pop feel"

prompt = f"""
あなたはプロの作詞家兼音楽プロデューサーです。
AI音楽生成モデル「YuE2」に投入するための、楽曲の「タイトル」「スタイルプロンプト（英語）」「日本語歌詞」「テーマ解説」を生成してください。

【現在のシチュエーション】
- 季節感: 秋（夕暮れから夜へ）
- サウンドスタイル提案: {genre_sample}
- ボーカル提案: {vocal_sample}

【楽曲の演奏時間（最重要制限）】
- 楽曲の長さが『最大2分58秒（178秒以内・目標2分30秒〜2分58秒）』に収まるよう、歌詞の構成と長さを厳密に設計してください。
- 短すぎず（1分台不可）、3分を超えない最適なボリュームにしてください。

【YuE2向け歌詞のセクション構成ルール】
以下の構成タグを必ずこの順序で使用してください：
[Intro]       : 曲の世界観を示す短い言葉やハミング（1〜2行）
[Verse 1]     : Aメロ①（情景や心理の描写、2〜3行）
[Pre-Chorus]  : Bメロ①（サビへの助走・感情の高まり、2行）
[Chorus]      : サビ①（感情のコア・最もキャッチーな主旋律、3〜4行）
[Verse 2]     : Aメロ②（ストーリーの進展、2〜3行）
[Chorus]      : サビ②（盛り上がり、3〜4行）
[Outro]       : アウトロ（静かな余韻・フェードアウト、1〜2行）
※3分を超えないよう [Bridge] や [Solo] などの過剰なセクションは追加しないでください。
※全体の合計行数は 16〜22行 程度に収めてください。各行は日本語として美しく自然な言葉遣いにすること。

【スタイルプロンプトのルール】
- 英語で記述すること。
- 指定のサウンドスタイルとボーカルをベースにしつつ、楽器、BPM、ムードを具体的に英語プロンプトに落とし込むこと。

【タイトルのルール】
- 英語またはローマ字の短いユニークな曲名（アンダースコア区切り、英数字のみ、例: midnight_drive, shibuya_lights）。
- 特定の単語に偏らず、楽曲のテーマに応じたオリジナリティ溢れるタイトルにすること。
"""

print("Gemini API (gemini-3.8-flash) で自律作詞テストを実行中...")
try:
    response = client.models.generate_content(
        model="gemini-3.8-flash",
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=SongGenerationPlan,
            temperature=0.85,
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
    print(f"Gemini API エラー: {e}")
