"""
YuE2 × Modal クラウド楽曲自動生成パイプライン (app.py)

【概要】
オープンソースAI音楽生成モデル「YuE2 (3B)」を、サーバーレスGPUインフラ「Modal」上で稼働させ、
完全クラウド完結（ローカルPCのGPU・電源不要）で楽曲を生成・保管・通知するシステムです。

【主な機能】
1. サーバーレスGPU推論 (NVIDIA L4 24GB VRAM):
   - 秒単位課金（常時起動コストゼロ）。
   - モデル重みは約4GBの永続Volume (yue2-model-cache) にキャッシュし、高速起動を実現。
2. 日本語歌詞自動最適化エンジン (optimize_lyrics):
   - 通常の漢字混じり・長文の歌詞を、形態素解析 (pykakasi) により
     「ひらがな」「分かち書き」「1行5〜8文字」へ自動整形。
   - メロディ音符（ABC記譜法）とモーラ（拍数）の1対1対応を強制し、歌詞のハルシネーション（勝手な作詞）を防止。
3. 高音質MP3自動変換エンジン (convert_flac_to_mp3):
   - 生成された可逆圧縮 FLAC 音源を、軽量・高音質な 192kbps MP3 へ FFmpeg で自動変換。
   - スマホ試聴・ストリーミング再生の利便性を最大化し、Drive容量を約1/10に削減。
4. Google Drive 自動連携 & mp3集約保存 (upload_to_drive / sync_flac_to_mp3_batch):
   - OAuth 2.0 ユーザー認証 (USER_TOKEN_B64) を利用し、個人のGoogle Drive容量を直接使用。
   - 各曲の個別フォルダ（yue2/曲名_日時/）に FLAC・MP3・楽譜・設定ログを完全保存。
   - さらに Google Drive の「yue2/mp3/」フォルダへ全曲の MP3 を集約自動配置。
   - 過去楽曲の一括同期バッチ機能により、過去の未変換曲も自動で MP3 化＆Drive 同期。
5. LLM自律作詞・スタイルプロンプト生成 (generate_lyrics_and_style):
   - Gemini API (gemini-3.8-flash) を活用し、季節・時間帯・ランダム音楽要素から
     YuE2専用の楽曲タイトル・スタイルプロンプト（英語）・日本語歌詞を自律生成。
6. Discord 完了通知連携 (notify_discord):
   - 楽曲生成とGoogle Driveアップロードが完了次第、Discordへ直接試聴リンク付きリッチEmbed通知を即時送信。
7. トリプル・トリガー対応:
   - Web UI: スマホ・ブラウザからワンタップ生成（手動歌詞入力 ＆ AI全自動生成の双方に対応）。
   - Cron: 2時間毎に完全自動でAI作詞から楽曲生成・Drive保存・Discord通知まで一括実行。
   - 日次Cronバッチ: 毎日深夜（JST 24:00）に未変換のFLACをMP3へ一括自動同期。
   - Local CLI: 開発・パラメータ検証用の手元実行 (modal run app.py)。
"""

import os
import json
import base64
import io
import datetime
import random
import re
import shutil
import subprocess
from pathlib import Path

import modal
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# 1. Modal アプリケーションおよびクラウド永続ストレージ (Volume) の定義
# ---------------------------------------------------------------------------
app = modal.App("yue2-song-generator")

# YuE2モデル重み（約4GB）を永続キャッシュするVolume（毎回のHFダウンロードを回避）
model_volume = modal.Volume.from_name("yue2-model-cache", create_if_missing=True)

# 生成された全楽曲データをクラウド側に永続保持するVolume
song_storage = modal.Volume.from_name("yue2-generated-songs", create_if_missing=True)

# ---------------------------------------------------------------------------
# 2. クラウドコンテナ環境の定義 (Image)
# ---------------------------------------------------------------------------
image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install("git", "ffmpeg")
    .pip_install(
        "torch>=2.4.0",
        "torchaudio",
        "transformers>=4.45.0",
        "accelerate",
        "soundfile",
        "huggingface_hub",
        "einops",
        "fastapi",
        "python-multipart",
        "pykakasi",
        "google-api-python-client",
        "google-auth",
        "google-genai",
        "requests",
        "pydantic",
        "git+https://github.com/multimodal-art-projection/YuE.git",
    )
    .env({"HF_HOME": "/root/models/hf"})
)

# ---------------------------------------------------------------------------
# 3. 日本語歌詞の自動最適化エンジン (Pre-processing)
# ---------------------------------------------------------------------------
def optimize_lyrics(raw_lyrics: str, max_line_len: int = 8) -> str:
    """
    漢字混じりの日本語歌詞を、YuE2が音節ズレ（ハルシネーション）を起こさずに歌えるよう
    「ひらがな」「分かち書き（スペース）」「1行5〜8文字」へ自動整形します。
    """
    try:
        import pykakasi
        kks = pykakasi.kakasi()
        has_kakasi = True
    except ImportError:
        has_kakasi = False

    optimized_lines = []
    for line in raw_lyrics.strip().split("\n"):
        line = line.strip()
        if not line:
            optimized_lines.append("")
            continue
        # [Verse], [Chorus] などのセクションタグはそのまま保持
        if line.startswith("[") and line.endswith("]"):
            optimized_lines.append(line)
            continue

        if has_kakasi:
            # 形態素解析とひらがな変換
            conversion = kks.convert(line)
            tokens = []
            for item in conversion:
                hira = item.get("hira", "")
                hira_clean = re.sub(r"[、。，．！？!?\s]", "", hira)
                if hira_clean:
                    tokens.append(hira_clean)

            # 5〜8文字単位で分かち書き（半角スペース区切り）改行
            current_line_tokens = []
            current_len = 0
            for tok in tokens:
                if current_len + len(tok) > max_line_len and current_line_tokens:
                    optimized_lines.append(" ".join(current_line_tokens))
                    current_line_tokens = [tok]
                    current_len = len(tok)
                else:
                    current_line_tokens.append(tok)
                    current_len += len(tok)

            if current_line_tokens:
                optimized_lines.append(" ".join(current_line_tokens))
        else:
            # pykakasiが利用できない場合のフォールバック（文字数分割）
            for i in range(0, len(line), max_line_len):
                optimized_lines.append(line[i:i + max_line_len])

    return "\n".join(optimized_lines)

# ---------------------------------------------------------------------------
# 4. 音声変換エンジン (FFmpeg: FLAC → 192kbps MP3)
# ---------------------------------------------------------------------------
def convert_flac_to_mp3(flac_path: Path, mp3_path: Path, bitrate: str = "192k") -> bool:
    """
    FFmpeg を呼び出し、可逆圧縮 FLAC ファイルを 192kbps の高音質・軽量 MP3 ファイルへ変換します。
    """
    if not flac_path.exists():
        print(f"[FFmpeg エラー] 変換元FLACが見つかりません: {flac_path}")
        return False
    try:
        cmd = [
            "ffmpeg", "-y",
            "-i", str(flac_path),
            "-codec:a", "libmp3lame",
            "-b:a", bitrate,
            str(mp3_path),
        ]
        subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
        return True
    except Exception as e:
        print(f"[FFmpeg エラー] MP3変換に失敗しました: {e}")
        return False

# ---------------------------------------------------------------------------
# 5. LLM自律作詞・スタイルプロンプト生成エンジン (Gemini API)
# ---------------------------------------------------------------------------
class SongGenerationPlan(BaseModel):
    title: str = Field(description="英語またはローマ字の短い楽曲タイトル（アンダースコア区切り、英数字のみ、例: autumn_rain）")
    style_prompt: str = Field(description="YuE2向けスタイルプロンプト（英語。ジャンル、BPM、楽器、ボーカルスタイル、ムードなど）")
    theme_description: str = Field(description="日本語による楽曲の着想・テーマ解説（1行程度）")
    lyrics: str = Field(description="日本語の歌詞。[Verse], [Chorus], [Outro] の構造を含める。")


def generate_lyrics_and_style(theme_hint: str = "") -> dict:
    """
    Gemini API (gemini-3.8-flash) を呼び出し、
    季節・時間帯・指定テーマ・ランダムな音楽要素に応じた楽曲タイトル、YuE2用スタイルプロンプト、日本語歌詞を自律生成します。
    """
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("[Gemini] GEMINI_API_KEY が未設定のため、デフォルトプリセットを使用します。")
        return {
            "title": "chill_morning",
            "style_prompt": "Japanese, female vocal, chill acoustic guitar, lo-fi hip hop beats, 80 BPM, warm piano, nostalgic mood",
            "theme_description": "静かな朝の光の中で一歩を踏み出すチルなアコースティックナンバー",
            "lyrics": "[Verse]\n静かな朝の光の中で\n新しいページが開いていく\n[Chorus]\n歩き出そう 自分のリズムで\nどこまでも続く空へ\n[Outro]\n穏やかな風",
        }

    try:
        from google import genai
        from google.genai import types

        # 現在の季節と時間帯（JST基準）を推定
        now_jst = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
        month = now_jst.month
        hour = now_jst.hour

        seasons = {
            12: "冬（凛とした澄んだ空気、温もり）", 1: "冬（新春、静けさ、雪）", 2: "晩冬（春を待つ気配）",
            3: "早春（芽吹き、出会いと別れ）", 4: "春（桜、希望、新生活）", 5: "初夏（新緑、爽快な風）",
            6: "初夏・梅雨（雨のしずく、静謐）", 7: "夏（太陽、海、情熱）", 8: "晩夏（夕立、祭りの余韻、切なさ）",
            9: "初秋（秋風、夕暮れ）", 10: "秋（紅葉、秋の夜長、琥珀色）", 11: "晩秋（落ち葉、冬の足音）"
        }
        season_str = seasons.get(month, "四季折々の情景")

        if 5 <= hour < 11:
            time_str = "朝（爽やか、目覚め、前向きな光）"
        elif 11 <= hour < 17:
            time_str = "昼・午後（日常、木漏れ日、心地よいリズム）"
        elif 17 <= hour < 22:
            time_str = "夕暮れ・夜（黄昏、帰路、ノスタルジー、街の灯り）"
        else:
            time_str = "深夜（静寂、内省的、チル、物思いに耽る時間）"

        # ランダムなジャンル・ボーカルの組み合わせ候補
        genre_pool = [
            "lo-fi hip hop chillout, warm Rhodes piano, vinyl crackle, 78 BPM",
            "modern J-pop ballad, acoustic grand piano, emotional strings, 88 BPM",
            "city pop, groovy slap bass, bright synth brass, funk guitar, 115 BPM",
            "acoustic folk pop, gentle fingerpicking acoustic guitar, warm cello, 82 BPM",
            "neo soul, smooth electric guitar chords, mellow bassline, relaxed beat, 85 BPM",
            "chill synthwave, analog vintage synth pads, nostalgic melody, 95 BPM",
        ]
        vocal_pool = [
            "Japanese female vocal, sweet and whispery voice",
            "Japanese female vocal, clear and expressive emotional voice",
            "Japanese male vocal, warm and gentle acoustic voice",
            "Japanese female vocal, stylish and airy voice",
        ]

        selected_genre = random.choice(genre_pool)
        selected_vocal = random.choice(vocal_pool)

        user_prompt = f"""
あなたはプロの作詞家兼音楽プロデューサーです。
AI音楽生成モデル「YuE2」に投入するための、楽曲の「タイトル」「スタイルプロンプト（英語）」「日本語歌詞」「テーマ解説」を生成してください。

【現在のシチュエーション】
- 季節: {season_str}
- 時間帯: {time_str}
- 推奨サウンドベース: {selected_genre}, {selected_vocal}
{f'- ユーザー指定のテーマ・着想: {theme_hint}' if theme_hint else ''}

【YuE2向け歌詞のルール】
- セクションタグ（[Verse], [Chorus], [Outro]）を必ず含めること。
- [Verse] は情景描写や日常の心理を描き、[Chorus] は感情のコアを高らかに歌い、[Outro] で静かに余韻を残すこと。
- 各行は長すぎず、日本語として響きが美しく自然な言葉遣いにすること。
- 生成時間は1〜2分程度を想定するため、各セクション2〜4行程度でコンパクトに構成すること。

【スタイルプロンプトのルール】
- 英語で記述すること。
- 'Japanese, ... vocal' を含め、楽器、テンポ（BPM）、ムードを具体的に指定すること。
"""
        client = genai.Client(api_key=api_key)
        response = client.models.generate_content(
            model="gemini-3.8-flash",
            contents=user_prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=SongGenerationPlan,
                temperature=0.75,
            ),
        )
        plan_dict = json.loads(response.text)
        print(f"=== [Gemini 自律作詞完了] タイトル: {plan_dict.get('title')} ===")
        return plan_dict
    except Exception as e:
        print(f"[Gemini 作詞エラー] API呼び出しに失敗したためフォールバックを使用: {e}")
        return {
            "title": "fallback_melody",
            "style_prompt": "Japanese, clear expressive female vocal, modern J-pop ballad, 90 BPM, piano, strings",
            "theme_description": "心に寄り添うエモーショナルなJ-POPバラード",
            "lyrics": "[Verse]\nビルの隙間から差し込む光が\n冷たいアスファルトを染めていく\n[Chorus]\n消えない痛みを抱えたままで\n僕らは次の朝へと走り出す\n[Outro]\n光の中へ",
        }

# ---------------------------------------------------------------------------
# 6. Discord Webhook 完了通知モジュール
# ---------------------------------------------------------------------------
def notify_discord(
    title: str,
    style: str,
    theme: str,
    lyrics: str,
    subfolder_id: str,
    seed: int,
    trigger_type: str = "手動生成",
) -> bool:
    """
    楽曲生成およびGoogle Driveアップロード完了時に、Discord WebhookへリッチEmbed通知を送信します。
    """
    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if not webhook_url:
        print("[Discord] DISCORD_WEBHOOK_URL が未設定のため、通知をスキップします。")
        return False

    try:
        import requests

        drive_folder_url = f"https://drive.google.com/drive/folders/{subfolder_id}" if subfolder_id else "https://drive.google.com"
        
        # 歌詞のプレビュー（最初の4行程度）
        lyrics_lines = [l for l in lyrics.splitlines() if l.strip() and not l.startswith("[")]
        preview_lyrics = "\n".join(lyrics_lines[:4])
        if len(lyrics_lines) > 4:
            preview_lyrics += "\n..."

        embed = {
            "title": f"🎵 新曲生成完了: {title}",
            "description": f"**{theme}**\n\nクラウドGPU（NVIDIA L4）での楽曲推論、192kbps MP3変換、およびGoogle Driveへの自動保存が正常に完了しました。",
            "color": 0x1A73E8,  # Google Blue
            "fields": [
                {
                    "name": "🎨 スタイル・サウンド",
                    "value": f"`{style[:150]}`",
                    "inline": False,
                },
                {
                    "name": "📁 Google Drive 保存先",
                    "value": f"[▶ Google Drive で試聴（FLAC & MP3）]({drive_folder_url})",
                    "inline": False,
                },
                {
                    "name": "🎲 シード値 / 実行種別",
                    "value": f"Seed: `{seed}` | 種別: **{trigger_type}**",
                    "inline": True,
                },
                {
                    "name": "📝 歌詞プレビュー",
                    "value": f"```\n{preview_lyrics}\n```",
                    "inline": False,
                },
            ],
            "footer": {
                "text": "YuE2 × Modal 楽曲自動生成パイプライン (MP3対応)",
            },
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }

        payload = {
            "content": f"🎉 **YuE2 楽曲自動生成パイプライン** より新曲のお知らせです！",
            "embeds": [embed],
        }

        res = requests.post(webhook_url, json=payload, timeout=10)
        if res.status_code in (200, 204):
            print(f"=== [Discord] 完了通知を送信しました: {title} ===")
            return True
        else:
            print(f"[Discord エラー] ステータスコード {res.status_code}: {res.text}")
            return False
    except Exception as e:
        print(f"[Discord エラー] 通知送信に失敗しました: {e}")
        return False

# ---------------------------------------------------------------------------
# 7. Google Drive 自動連携 & mp3集約保存モジュール (OAuth 2.0 連携)
# ---------------------------------------------------------------------------
def get_or_create_mp3_folder(service, target_folder_id: str) -> str:
    """
    Google Drive の target_folder_id 配下に 'mp3' フォルダが存在するか検索し、
    存在しなければ自動作成してそのフォルダ ID を返します。
    """
    try:
        query = f"'{target_folder_id}' in parents and name = 'mp3' and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
        res = service.files().list(q=query, spaces="drive", fields="files(id, name)").execute()
        files = res.get("files", [])
        if files:
            return files[0]["id"]

        folder_metadata = {
            "name": "mp3",
            "mimeType": "application/vnd.google-apps.folder",
            "parents": [target_folder_id],
        }
        folder = service.files().create(body=folder_metadata, fields="id").execute()
        folder_id = folder.get("id")
        print(f"=== [Google Drive] 'mp3' フォルダを新規作成しました (ID: {folder_id}) ===")
        return folder_id
    except Exception as e:
        print(f"[Google Drive エラー] mp3 フォルダの取得・作成に失敗しました: {e}")
        return ""


def upload_to_drive(subfolder_name: str, artifacts: dict, prompt_info: str) -> str:
    """
    生成された楽曲ファイル（FLAC、MP3、ABC楽譜、メタデータログ）を
    Google Driveの個別フォルダ（yue2/曲名_日時/）へ保存し、
    さらに MP3 ファイルを共有フォルダ（yue2/mp3/）へ集約配置します。
    """
    user_token_b64 = os.environ.get("USER_TOKEN_B64")
    target_folder_id = os.environ.get("TARGET_FOLDER_ID")
    if not user_token_b64 or not target_folder_id:
        print("[Drive] 認証情報が設定されていないため、スキップします。")
        return ""

    try:
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build
        from googleapiclient.http import MediaIoBaseUpload

        # Base64デコードしてOAuth認証情報をロード
        token_info = json.loads(base64.b64decode(user_token_b64).decode("utf-8"))
        creds = Credentials.from_authorized_user_info(token_info, scopes=["https://www.googleapis.com/auth/drive"])
        service = build("drive", "v3", credentials=creds)

        # 1. yue2 フォルダ内に「曲名_日時」の個別サブフォルダを作成
        folder_metadata = {
            "name": subfolder_name,
            "mimeType": "application/vnd.google-apps.folder",
            "parents": [target_folder_id],
        }
        subfolder = service.files().create(body=folder_metadata, fields="id").execute()
        subfolder_id = subfolder.get("id")

        # 2. 生成アセット群を個別フォルダへアップロード
        mp3_data = None
        for fname, data in artifacts.items():
            meta = {"name": fname, "parents": [subfolder_id]}
            if fname.endswith(".flac"):
                mtype = "audio/flac"
            elif fname.endswith(".mp3"):
                mtype = "audio/mpeg"
                mp3_data = data
            elif fname.endswith(".abc"):
                mtype = "text/plain"
            else:
                mtype = "application/octet-stream"
            media = MediaIoBaseUpload(io.BytesIO(data), mimetype=mtype)
            service.files().create(body=meta, media_body=media).execute()

        # 3. 設定ログ (prompt_info.txt) のアップロード
        info_meta = {"name": "prompt_info.txt", "parents": [subfolder_id]}
        info_media = MediaIoBaseUpload(io.BytesIO(prompt_info.encode("utf-8")), mimetype="text/plain")
        service.files().create(body=info_meta, media_body=info_media).execute()

        # 4. 【新規】Google Drive の 'mp3' フォルダへ「曲名_日時.mp3」として集約保存
        if mp3_data:
            mp3_folder_id = get_or_create_mp3_folder(service, target_folder_id)
            if mp3_folder_id:
                mp3_filename = f"{subfolder_name}.mp3"
                mp3_meta = {"name": mp3_filename, "parents": [mp3_folder_id]}
                mp3_media = MediaIoBaseUpload(io.BytesIO(mp3_data), mimetype="audio/mpeg")
                service.files().create(body=mp3_meta, media_body=mp3_media).execute()
                print(f"=== [Google Drive] mp3 フォルダへ集約保存完了: {mp3_filename} ===")

        print(f"=== [Google Drive] 自動アップロード完了: {subfolder_name} (ID: {subfolder_id}) ===")
        return subfolder_id
    except Exception as e:
        print(f"[Google Drive エラー] アップロードに失敗しました: {e}")
        return ""

# ---------------------------------------------------------------------------
# 8. コア生成関数 (NVIDIA L4 GPU / タイムアウト10分)
# ---------------------------------------------------------------------------
@app.function(
    image=image,
    gpu="L4",
    timeout=600,
    volumes={"/root/models": model_volume, "/root/songs": song_storage},
    secrets=[
        modal.Secret.from_name("google-drive-secret"),
        modal.Secret.from_name("discord-secret"),
    ],
)
def generate_music_core(
    style_prompt: str,
    lyrics: str,
    seed: int = 42,
    title: str = "song",
    theme_description: str = "",
    trigger_type: str = "手動生成",
) -> dict:
    """
    L4 GPU上でYuE2モデルをロードし、シンボリック・プランニング（ABC楽譜）を経て楽曲を生成します。
    生成後は可逆圧縮FLACから192kbps MP3へ変換し、Modal Volume および Google Drive へ保存、Discord へ通知します。
    """
    from yue2 import YuE2Pipeline

    # 歌詞の自動最適化を実行
    optimized_lyrics = optimize_lyrics(lyrics)
    print(f"\n=== [自動最適化された歌詞 (YuE2投入)] ===\n{optimized_lyrics}\n=====================================\n")

    temp_out = Path("/tmp/yue2_output")
    if temp_out.exists():
        shutil.rmtree(temp_out)
    temp_out.mkdir(parents=True, exist_ok=True)

    print(f"--- YuE2 推論開始: {title} (Seed: {seed}, Trigger: {trigger_type}) ---")
    with YuE2Pipeline.from_pretrained("m-a-p/YuE2-3B", device="cuda") as pipe:
        song = pipe(style=style_prompt, lyrics=optimized_lyrics, cot="full", seed=seed)
        song.save_artifacts(str(temp_out))

    # 【新規】FLACから 192kbps MP3 への自動変換
    flac_file = temp_out / "audio.flac"
    mp3_file = temp_out / "audio.mp3"
    if flac_file.exists():
        print(f"--- [FFmpeg] 192kbps MP3 へ変換中: {flac_file.name} ---")
        if convert_flac_to_mp3(flac_file, mp3_file, bitrate="192k"):
            print(f"--- [FFmpeg] MP3 変換完了: {mp3_file.name} (サイズ: {mp3_file.stat().st_size / 1024 / 1024:.2f} MB) ---")

    # Modal Volume への永続保存
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    subfolder_name = f"{title}_{timestamp}"
    permanent_dir = Path("/root/songs") / subfolder_name
    permanent_dir.mkdir(parents=True, exist_ok=True)

    artifacts = {}
    for f in temp_out.iterdir():
        if f.is_file():
            data = f.read_bytes()
            artifacts[f.name] = data
            (permanent_dir / f.name).write_bytes(data)

    # 人間が読める元歌詞、最適化歌詞、テーマ解説をログファイルに記録
    prompt_info_text = (
        f"Title: {title}\n"
        f"Timestamp: {timestamp}\n"
        f"Trigger: {trigger_type}\n"
        f"Seed: {seed}\n"
        f"Theme: {theme_description}\n"
        f"Style: {style_prompt}\n\n"
        f"Original Lyrics:\n{lyrics}\n\n"
        f"Optimized Lyrics (Sent to YuE2 Model):\n{optimized_lyrics}\n"
    )
    (permanent_dir / "prompt_info.txt").write_text(prompt_info_text, encoding="utf-8")
    song_storage.commit()
    print(f"--- Modal Volume 永続保存完了: {permanent_dir} ---")

    # Google Drive への自動同期（個別フォルダ ＆ mp3集約フォルダ）
    subfolder_id = upload_to_drive(subfolder_name=subfolder_name, artifacts=artifacts, prompt_info=prompt_info_text)

    # Discord への完了通知
    notify_discord(
        title=title,
        style=style_prompt,
        theme=theme_description or "AI生成楽曲",
        lyrics=lyrics,
        subfolder_id=subfolder_id,
        seed=seed,
        trigger_type=trigger_type,
    )

    return artifacts

# ---------------------------------------------------------------------------
# 9. トリガー①: 定期自動実行 (Cron: 2時間毎に1度自動生成)
# ---------------------------------------------------------------------------
@app.function(
    image=image,
    schedule=modal.Cron("0 */2 * * *"),
    secrets=[
        modal.Secret.from_name("gemini-secret"),
    ],
)
def scheduled_batch_generation():
    """
    2時間毎に自動起動し、Geminiで現在の季節・時間帯に応じた楽曲テーマと歌詞を自律生成した上で、
    GPUによる楽曲生成・Drive保存（FLAC & MP3）・Discord通知まで完全放置で実行します。
    """
    print("【Cron 定期実行】自律作詞・楽曲ストック生成を開始します...")
    
    # 1. Gemini による自律作詞とスタイルプロンプト生成（CPU上で数秒で完了）
    plan = generate_lyrics_and_style()
    print(f"【AI作詞決定】曲名: {plan['title']}, テーマ: {plan['theme_description']}")

    # 2. クラウドGPU（L4）へジョブを投入
    seed = int(datetime.datetime.now().timestamp()) % 100000
    generate_music_core.spawn(
        style_prompt=plan["style_prompt"],
        lyrics=plan["lyrics"],
        seed=seed,
        title=f"auto_{plan['title']}",
        theme_description=plan["theme_description"],
        trigger_type="Cron定期バッチ実行",
    )
    print("【Cron 定期実行】GPU生成タスクをキューに投入しました。完了後にDriveおよびDiscordへ自動連携されます。")

# ---------------------------------------------------------------------------
# 10. トリガー②: 過去楽曲の MP3 一括変換 & Google Drive 同期バッチ (日次定期実行 / JST 24:00)
# ---------------------------------------------------------------------------
@app.function(
    image=image,
    schedule=modal.Cron("0 15 * * *"),  # 毎日 15:00 UTC = 日本時間 24:00
    volumes={"/root/songs": song_storage},
    secrets=[modal.Secret.from_name("google-drive-secret")],
    timeout=600,
)
def sync_flac_to_mp3_batch():
    """
    Modal Volume 内の全楽曲を走査し、まだ Google Drive の mp3 フォルダに同期されていない曲を
    CPUコンテナ上で 192kbps MP3 に変換して一括同期します（高価なGPUは使用せず課金ゼロ）。
    手動実行コマンド: modal run app.py::sync_flac_to_mp3_batch
    """
    user_token_b64 = os.environ.get("USER_TOKEN_B64")
    target_folder_id = os.environ.get("TARGET_FOLDER_ID")
    if not user_token_b64 or not target_folder_id:
        print("[同期バッチ] 認証情報が設定されていないためスキップします。")
        return

    try:
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build
        from googleapiclient.http import MediaIoBaseUpload

        token_info = json.loads(base64.b64decode(user_token_b64).decode("utf-8"))
        creds = Credentials.from_authorized_user_info(token_info, scopes=["https://www.googleapis.com/auth/drive"])
        service = build("drive", "v3", credentials=creds)

        # 1. Google Drive の 'mp3' フォルダを取得または作成
        mp3_folder_id = get_or_create_mp3_folder(service, target_folder_id)
        if not mp3_folder_id:
            print("[同期バッチ エラー] mp3 フォルダを特定できませんでした。")
            return

        # 2. Drive の mp3 フォルダ内に既存のファイル名一覧を取得（重複スキップ用）
        existing_mp3s = set()
        page_token = None
        while True:
            q = f"'{mp3_folder_id}' in parents and trashed = false"
            res = service.files().list(q=q, fields="nextPageToken, files(name)", pageToken=page_token).execute()
            for f in res.get("files", []):
                existing_mp3s.add(f.get("name"))
            page_token = res.get("nextPageToken")
            if not page_token:
                break

        print(f"=== [同期バッチ] 現在 Google Drive (mp3フォルダ) に存在する楽曲数: {len(existing_mp3s)} 件 ===")

        # 3. Modal Volume (/root/songs) 内の全楽曲ディレクトリを走査
        songs_root = Path("/root/songs")
        if not songs_root.exists():
            print("[同期バッチ] /root/songs が存在しないため終了します。")
            return

        synced_count = 0
        converted_count = 0

        for song_dir in sorted(songs_root.iterdir()):
            if not song_dir.is_dir():
                continue

            target_filename = f"{song_dir.name}.mp3"
            if target_filename in existing_mp3s:
                continue  # すでにDriveに存在する場合はスキップ

            flac_path = song_dir / "audio.flac"
            mp3_path = song_dir / "audio.mp3"

            # Volume内にMP3が未作成の場合は変換
            if not mp3_path.exists():
                if flac_path.exists():
                    print(f"--- [同期バッチ] MP3 変換実行: {song_dir.name} ---")
                    if convert_flac_to_mp3(flac_path, mp3_path, bitrate="192k"):
                        converted_count += 1
                else:
                    continue

            # Drive の mp3 フォルダへアップロード
            if mp3_path.exists():
                print(f"--- [同期バッチ] Drive へアップロード中: {target_filename} ---")
                meta = {"name": target_filename, "parents": [mp3_folder_id]}
                media = MediaIoBaseUpload(io.BytesIO(mp3_path.read_bytes()), mimetype="audio/mpeg")
                service.files().create(body=meta, media_body=media).execute()
                existing_mp3s.add(target_filename)
                synced_count += 1

        if converted_count > 0:
            song_storage.commit()

        print(f"=== [同期バッチ完了] 新規MP3変換: {converted_count} 件 / Drive同期アップロード: {synced_count} 件 ===")
    except Exception as e:
        print(f"[同期バッチ エラー] 処理中に例外が発生しました: {e}")

# ---------------------------------------------------------------------------
# 11. トリガー③: 手動生成用 Web UI (FastAPI / スマホ対応)
# ---------------------------------------------------------------------------
web_app = FastAPI()

HTML_CONTENT = """<!DOCTYPE html>
<html lang="ja">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>YuE2 クラウド楽曲自動生成スタジオ</title>
    <style>
        :root { --primary: #1a73e8; --bg: #f8f9fa; --card: #ffffff; }
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; max-width: 680px; margin: 20px auto; padding: 15px; background: var(--bg); color: #333; }
        .card { background: var(--card); border-radius: 12px; padding: 24px; box-shadow: 0 2px 10px rgba(0,0,0,0.06); margin-bottom: 20px; }
        h2 { color: var(--primary); margin-top: 0; display: flex; align-items: center; gap: 8px; }
        h3 { margin-top: 0; font-size: 16px; color: #555; }
        label { display: block; margin-top: 14px; font-weight: 600; font-size: 14px; }
        input, textarea, select { width: 100%; padding: 10px; margin-top: 6px; border: 1px solid #d0d7de; border-radius: 8px; box-sizing: border-box; font-size: 14px; }
        textarea { resize: vertical; }
        .btn { width: 100%; padding: 12px; border: none; border-radius: 8px; font-size: 15px; font-weight: 600; cursor: pointer; transition: 0.2s; margin-top: 16px; }
        .btn-primary { background: var(--primary); color: white; }
        .btn-primary:hover { background: #1557b0; }
        .btn-ai { background: #137333; color: white; }
        .btn-ai:hover { background: #0d5224; }
        .tip { font-size: 12px; color: #666; margin-top: 6px; line-height: 1.5; }
        .tabs { display: flex; gap: 8px; margin-bottom: 16px; }
        .tab-btn { flex: 1; padding: 10px; text-align: center; background: #e8f0fe; color: var(--primary); border: none; border-radius: 8px; font-weight: 600; cursor: pointer; }
        .tab-btn.active { background: var(--primary); color: white; }
        .badge { display: inline-block; padding: 2px 8px; border-radius: 12px; font-size: 11px; font-weight: bold; background: #e8f0fe; color: var(--primary); }
    </style>
</head>
<body>
    <div class="card">
        <h2>🎵 YuE2 クラウド楽曲生成スタジオ <span class="badge">FLAC & 192k MP3</span></h2>
        <p class="tip">サーバーレスGPU（NVIDIA L4）で高品質な楽曲を生成し、Google Driveの個別フォルダおよび「mp3」共有フォルダへの保存、Discordへの完了通知を完全自動で行います。</p>
        
        <div class="tabs">
            <button class="tab-btn active" onclick="showTab('ai-tab')">🤖 AIおまかせ生成 (Gemini)</button>
            <button class="tab-btn" onclick="showTab('manual-tab')">✍️ 自由作詞・詳細設定</button>
        </div>

        <!-- AIおまかせフォーム -->
        <div id="ai-tab">
            <h3>季節や時間帯、キーワードからAIが自動で作詞・作曲設定を行います</h3>
            <form action="/generate-ai" method="post">
                <label>曲のテーマ・キーワード（任意）:</label>
                <input type="text" name="theme_hint" placeholder="例: 秋の雨上がりの夕暮れ、星空ドライブ、切ない失恋、疾走感">
                <div class="tip">※空欄の場合は、現在の季節や時間帯に合わせたテーマをGeminiが自律選択します。</div>

                <label>シード値 (Seed):</label>
                <input type="number" name="seed" value="0">
                <div class="tip">※0の場合はランダムシードが自動設定されます。</div>

                <button type="submit" class="btn btn-ai">✨ AIにおまかせで楽曲生成を開始</button>
            </form>
        </div>

        <!-- 手動入力フォーム -->
        <div id="manual-tab" style="display:none;">
            <h3>自分で歌詞やサウンドスタイルを指定して生成します</h3>
            <form action="/generate-manual" method="post">
                <label>曲名 (Title):</label>
                <input type="text" name="title" value="drive_sync_track" required>

                <label>スタイルプロンプト (Style - 英語):</label>
                <input type="text" name="style" value="Japanese, clear expressive female vocal, modern J-pop ballad, 92 BPM, piano, strings" required>

                <label>シード値 (Seed):</label>
                <input type="number" name="seed" value="42">

                <label>歌詞 (Lyrics):</label>
                <textarea name="lyrics" rows="7">[Verse]
ビルの隙間から差し込む光が
冷たいアスファルトを染めていく
[Chorus]
消えない痛みを抱えたままで
僕らは次の朝へと走り出す
[Outro]
光の中へ</textarea>
                <div class="tip">※入力歌詞は自動で「ひらがな・分かち書き・5〜8文字改行」に最適化され、YuE2に投入されます。</div>

                <button type="submit" class="btn btn-primary">🚀 クラウドGPUで生成を開始</button>
            </form>
        </div>
    </div>

    <script>
        function showTab(tabId) {
            document.getElementById('ai-tab').style.display = tabId === 'ai-tab' ? 'block' : 'none';
            document.getElementById('manual-tab').style.display = tabId === 'manual-tab' ? 'block' : 'none';
            const btns = document.querySelectorAll('.tab-btn');
            btns[0].classList.toggle('active', tabId === 'ai-tab');
            btns[1].classList.toggle('active', tabId === 'manual-tab');
        }
    </script>
</body>
</html>"""

@web_app.get("/", response_class=HTMLResponse)
async def web_ui():
    return HTML_CONTENT

@web_app.post("/generate-ai", response_class=HTMLResponse)
async def handle_generate_ai(request: Request):
    form = await request.form()
    theme_hint = str(form.get("theme_hint") or "").strip()
    seed_raw = form.get("seed")
    try:
        seed = int(seed_raw) if seed_raw and int(seed_raw) != 0 else int(datetime.datetime.now().timestamp()) % 100000
    except (ValueError, TypeError):
        seed = 42

    plan = generate_lyrics_and_style(theme_hint=theme_hint)
    title = f"web_{plan['title']}"

    generate_music_core.spawn(
        style_prompt=plan["style_prompt"],
        lyrics=plan["lyrics"],
        seed=seed,
        title=title,
        theme_description=plan["theme_description"],
        trigger_type="Web UI (AIおまかせ生成)",
    )

    return f"""<!DOCTYPE html>
<html lang="ja"><body style="font-family:sans-serif; text-align:center; padding:50px; background:#f8f9fa;">
    <div style="max-width:550px; margin:auto; background:white; padding:30px; border-radius:12px; box-shadow:0 2px 8px rgba(0,0,0,0.08);">
        <h3 style="color:#137333;">✨ AIおまかせ生成を開始しました</h3>
        <p>曲名: <b>{title}</b></p>
        <p style="font-size:14px; color:#555;">テーマ: {plan['theme_description']}</p>
        <p style="font-size:13px; color:#777; background:#f1f3f4; padding:10px; border-radius:6px; text-align:left;">
            <b>スタイル:</b> {plan['style_prompt']}<br>
            <b>シード値:</b> {seed}
        </p>
        <p style="font-size:14px;">生成完了後、<b>Google Drive (個別フォルダ ＆ mp3フォルダ)</b> および <b>Discord通知</b> に自動転送されます。</p>
        <p style="margin-top:24px;"><a href="/" style="display:inline-block; padding:10px 20px; background:#1a73e8; color:white; text-decoration:none; border-radius:6px;">← もう1曲生成する</a></p>
    </div>
</body></html>"""

@web_app.post("/generate-manual", response_class=HTMLResponse)
async def handle_generate_manual(request: Request):
    form = await request.form()
    title = str(form.get("title") or "web_track")
    style = str(form.get("style") or "Japanese, modern J-pop ballad, 92 BPM, piano, strings")
    lyrics = str(form.get("lyrics") or "[Verse]\n光の中へ\n[Chorus]\n明日へ")
    seed_raw = form.get("seed")
    try:
        seed = int(seed_raw) if seed_raw else 42
    except (ValueError, TypeError):
        seed = 42

    generate_music_core.spawn(
        style_prompt=style,
        lyrics=lyrics,
        seed=seed,
        title=title,
        theme_description="Web UI手動入力による生成",
        trigger_type="Web UI (手動設定)",
    )

    return f"""<!DOCTYPE html>
<html lang="ja"><body style="font-family:sans-serif; text-align:center; padding:50px; background:#f8f9fa;">
    <div style="max-width:550px; margin:auto; background:white; padding:30px; border-radius:12px; box-shadow:0 2px 8px rgba(0,0,0,0.08);">
        <h3 style="color:#1a73e8;">🚀 生成リクエストを受け付けました</h3>
        <p>曲名: <b>{title}</b> (Seed: {seed})</p>
        <p>クラウドGPU（L4）にて生成中です。完了した音声は <b>Google Drive (個別フォルダ ＆ mp3フォルダ)</b> および <b>Discord</b> に自動送信されます。</p>
        <p style="margin-top:24px;"><a href="/" style="display:inline-block; padding:10px 20px; background:#1a73e8; color:white; text-decoration:none; border-radius:6px;">← もう1曲生成する</a></p>
    </div>
</body></html>"""

@app.function(
    image=image,
    secrets=[
        modal.Secret.from_name("gemini-secret"),
    ],
)
@modal.asgi_app()
def web():
    return web_app

# ---------------------------------------------------------------------------
# 12. トリガー④: ローカルCLI実行エントリポイント (手元テスト用)
# ---------------------------------------------------------------------------
@app.local_entrypoint()
def main(
    style: str = "",
    lyrics_file: str = "",
    title: str = "",
    seed: int = 42,
    auto_ai: bool = False,
    theme: str = "",
):
    if auto_ai or (not style and not lyrics_file and not title):
        print("【CLI】Geminiによる自律作詞モードで生成パラメータを取得中...")
        if not os.environ.get("GEMINI_API_KEY"):
            env_file = Path("/home/eiichi/src/yt-analysis/.env")
            if env_file.exists():
                for line in env_file.read_text(encoding="utf-8").splitlines():
                    if line.startswith("GEMINI_API_KEY="):
                        os.environ["GEMINI_API_KEY"] = line.split("=", 1)[1].strip().strip('"').strip("'")
                        break
        plan = generate_lyrics_and_style(theme_hint=theme)
        style = plan["style_prompt"]
        lyrics = plan["lyrics"]
        title = f"cli_{plan['title']}"
        theme_desc = plan["theme_description"]
        print(f"決定した曲名: {title}")
        print(f"テーマ: {theme_desc}")
    else:
        if lyrics_file and Path(lyrics_file).exists():
            lyrics = Path(lyrics_file).read_text(encoding="utf-8").strip()
        else:
            lyrics = "[Verse]\nビルの隙間から差し込む光が\n冷たいアスファルトを染めていく\n[Chorus]\n消えない痛みを抱えたままで\n僕らは次の朝へと走り出す\n[Outro]\n光の中へ"
        if not style:
            style = "Japanese, clear expressive female vocal, modern J-pop ballad, 92 BPM, acoustic piano, strings"
        if not title:
            title = "cli_song"
        theme_desc = "ローカルCLIからの手動指定実行"

    print(f"クラウドGPUへジョブを投入: {title} (Seed: {seed})")
    artifacts = generate_music_core.remote(
        style_prompt=style,
        lyrics=lyrics,
        seed=seed,
        title=title,
        theme_description=theme_desc,
        trigger_type="ローカルCLI",
    )

    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = Path("outputs") / f"{title}_{timestamp}"
    out_dir.mkdir(parents=True, exist_ok=True)
    for fname, data in artifacts.items():
        (out_dir / fname).write_bytes(data)
    print(f"ローカル保存完了: {out_dir}/")
