"""
最新プロンプト（テーマプール・偏り防止ルール・自然な楽曲構成）の検証スクリプト
"""
import os
import json
from pathlib import Path

# yt-analysis/.env から GEMINI_API_KEY を取得（ローカルテスト用）
env_path = Path("/home/eiichi/src/yt-analysis/.env")
if env_path.exists():
    for line in env_path.read_text(encoding="utf-8").splitlines():
        if line.startswith("GEMINI_API_KEY="):
            os.environ["GEMINI_API_KEY"] = line.split("=", 1)[1].strip().strip('"').strip("'")
            break

from app import generate_lyrics_and_style

print("=== Gemini 自律作詞・スタイルプロンプト生成テスト ===")
for i in range(1, 4):
    print(f"\n--- [テスト生成 #{i}] ---")
    plan = generate_lyrics_and_style()
    print(f"タイトル: {plan['title']}")
    print(f"テーマ解説: {plan['theme_description']}")
    print(f"スタイル: {plan['style_prompt']}")
    print(f"歌詞抜粋:\n{plan['lyrics'][:150]}...\n")
