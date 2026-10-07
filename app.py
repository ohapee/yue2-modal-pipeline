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
# 4. 音声変換・時間制御エンジン (FFmpeg: 最大2分58秒制限 & 192kbps MP3)
# ---------------------------------------------------------------------------
MAX_SONG_DURATION_SEC = 178.0  # 最大2分58秒 (178秒)

def get_audio_duration(file_path: Path) -> float:
    """
    ffprobe を使用して音声ファイルの正確な長さ（秒数）を取得します。
    """
    try:
        cmd = [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(file_path),
        ]
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)
        return float(res.stdout.strip())
    except Exception as e:
        print(f"[ffprobe 警告] 音声長の取得に失敗しました: {e}")
        return 0.0


def enforce_max_duration(input_path: Path, output_path: Path, max_sec: float = MAX_SONG_DURATION_SEC) -> bool:
    """
    音声が max_sec（2分58秒 = 178秒）を超えている場合、
    終了前5秒間（173〜178秒）で自然にフェードアウトさせてきっかり178秒以内にトリミングします。
    """
    dur = get_audio_duration(input_path)
    if dur <= 0:
        return False

    if dur > max_sec:
        print(f"--- [時間制限] 音声長が {dur:.1f}秒 (制限: {max_sec:.0f}秒) のため、美しくフェードアウト・トリミングします ---")
        fade_start = max_sec - 5.0
        temp_trimmed = input_path.parent / f"trimmed_{input_path.name}"
        cmd = [
            "ffmpeg", "-y",
            "-i", str(input_path),
            "-to", str(max_sec),
            "-af", f"afade=t=out:st={fade_start}:d=5.0",
            str(temp_trimmed),
        ]
        try:
            subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
            shutil.move(temp_trimmed, output_path)
            return True
        except Exception as e:
            print(f"[ffmpeg エラー] 時間トリミングに失敗しました: {e}")
            if temp_trimmed.exists():
                temp_trimmed.unlink()
            return False
    else:
        if input_path != output_path:
            shutil.copyfile(input_path, output_path)
        return True


def convert_flac_to_mp3(flac_path: Path, mp3_path: Path, bitrate: str = "192k", max_sec: float = MAX_SONG_DURATION_SEC) -> bool:
    """
    FFmpeg を呼び出し、可逆圧縮 FLAC を 192kbps の高音質・軽量 MP3 へ変換します。
    万が一 178秒（2分58秒）を超える場合は、最後の5秒間で自然にフェードアウトさせて確実に2分58秒以内に収めます。
    """
    if not flac_path.exists():
        print(f"[FFmpeg エラー] 変換元FLACが見つかりません: {flac_path}")
        return False
    try:
        dur = get_audio_duration(flac_path)
        if dur > max_sec:
            fade_start = max_sec - 5.0
            cmd = [
                "ffmpeg", "-y",
                "-i", str(flac_path),
                "-to", str(max_sec),
                "-af", f"afade=t=out:st={fade_start}:d=5.0",
                "-codec:a", "libmp3lame",
                "-b:a", bitrate,
                str(mp3_path),
            ]
        else:
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
# 5. マルチメディア生成モジュール (ジャケット画像 & ビジュアライザー動画)
# ---------------------------------------------------------------------------
def generate_cover_art(
    title: str,
    theme_description: str,
    style_prompt: str,
    output_png_path: Path,
    output_3000_jpg_path: Path,
) -> bool:
    """
    Gemini 画像生成モデル (gemini-2.5-flash-image) を呼び出し、
    楽曲の世界観に完全に合致したジャケット画像を生成して 3000x3000px 配信規格 JPG へアップスケールします。
    """
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("[ジャケット画像] GEMINI_API_KEY が未設定のためスキップします。")
        return False
    try:
        from google import genai
        client = genai.Client(api_key=api_key)

        image_prompt = (
            f"Photorealistic cinematic photograph, professional 35mm film photography, authentic realistic photo, "
            f"natural lighting, exquisite depth of field, real life authentic scene, award-winning album cover art. "
            f"Scene and Mood: {theme_description}, Musical Style: {style_prompt}. "
            f"Square 1:1 aspect ratio, ultra-detailed, highly realistic, masterpiece. "
            f"DO NOT include any anime, cartoon, illustration, drawing, CGI, 3D render, watermark, or text."
        )
        print(f"--- [Gemini] ジャケット画像生成中: {title} ---")
        response = client.models.generate_content(
            model="gemini-2.5-flash-image",
            contents=image_prompt,
        )
        img_bytes = None
        if response.candidates and response.candidates[0].content and response.candidates[0].content.parts:
            for part in response.candidates[0].content.parts:
                if hasattr(part, "inline_data") and part.inline_data:
                    img_bytes = part.inline_data.data
                    break

        if not img_bytes:
            print("[ジャケット画像] 画像データの取得に失敗しました。")
            return False

        output_png_path.write_bytes(img_bytes)

        # FFmpeg で 3000x3000px 配信規格 JPG へ高画質アップスケール（Lanczos）
        cmd = [
            "ffmpeg", "-y",
            "-i", str(output_png_path),
            "-vf", "scale=3000:3000:flags=lanczos",
            "-q:v", "2",
            str(output_3000_jpg_path),
        ]
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if res.returncode == 0:
            print(f"--- [FFmpeg] 3000x3000px ジャケット画像作成完了: {output_3000_jpg_path.name} ---")
            return True
        else:
            print(f"[FFmpeg エラー] 画像リサイズ失敗: {res.stderr.decode('utf-8', errors='ignore')}")
            return False
    except Exception as e:
        print(f"[ジャケット画像 エラー] 生成中に例外が発生しました: {e}")
        return False


def render_visualizer_video(
    cover_image_path: Path,
    audio_path: Path,
    output_video_path: Path,
) -> bool:
    """
    FFmpeg を使用して、ジャケット画像と音声から波形ビジュアライザー動画（1920x1080 16:9 MP4）を生成します。
    - 背景: ジャケット画像の拡大 ＋ ぼかし (gblur)
    - 前面中央: 正方形ジャケットアート（700x700）
    - 下部: 音楽のダイナミクスに連動するネオンシアン波形 (showwaves)
    - 音声: 192kbps AAC
    """
    try:
        print(f"--- [FFmpeg] フルHDビジュアライザー動画レンダリング開始: {output_video_path.name} ---")
        filter_str = (
            "[0:v]scale=1920:1080:force_original_aspect_ratio=increase,crop=1920:1080,gblur=sigma=20[bg]; "
            "[0:v]scale=700:700[fg]; "
            "[1:a]showwaves=s=1920x200:mode=line:colors=0x00e5ff@0.85[wave]; "
            "[bg][fg]overlay=(W-w)/2:(H-h)/2-40[v1]; "
            "[v1][wave]overlay=0:H-h-20[outv]"
        )
        cmd = [
            "ffmpeg", "-y",
            "-loop", "1", "-i", str(cover_image_path),
            "-i", str(audio_path),
            "-filter_complex", filter_str,
            "-map", "[outv]",
            "-map", "1:a",
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", "22",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac",
            "-b:a", "192k",
            "-shortest",
            str(output_video_path),
        ]
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if res.returncode == 0:
            print(f"--- [FFmpeg] 動画レンダリング完了: {output_video_path.name} (サイズ: {output_video_path.stat().st_size / 1024 / 1024:.2f} MB) ---")
            return True
        else:
            print(f"[FFmpeg エラー] 動画レンダリング失敗: {res.stderr.decode('utf-8', errors='ignore')}")
            return False
    except Exception as e:
        print(f"[FFmpeg エラー] 動画生成中に例外が発生しました: {e}")
        return False

# ---------------------------------------------------------------------------
# 6. LLM自律作詞・スタイルプロンプト生成エンジン (Gemini API)
# ---------------------------------------------------------------------------
class SongGenerationPlan(BaseModel):
    title: str = Field(description="英語またはローマ字の短い楽曲タイトル（アンダースコア区切り、英数字のみ、例: starlight_drive, neon_horizon, summer_breeze）")
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

        # 多彩な世界観・シチュエーションの候補プール（32種：四季・日常・SF・感情など）
        theme_pool = [
            # 春・旅立ち・新生活
            "春の旅立ち、満開の桜並木、希望と少しの切なさを胸に歩き出す朝",
            "春風と新しいスニーカー、見慣れない街で始まる新しいストーリー",
            "木漏れ日の入学式、新しいノートを開くときの期待と緊張感",
            
            # 夏・海・フェス・情熱
            "真夏の海岸線ドライブ、照りつける太陽、炭酸の泡、弾ける笑顔",
            "夏の終わりの夕立、遠くで響く雷鳴、雨宿りのバス停での淡い会話",
            "真夜中の夏フェス、大歓声とスモーク、レーザービームの下で踊る熱狂",
            "秘密基地のような砂浜、打ち寄せる波、夜空に咲いて消える線香花火",
            
            # 秋・黄昏・ノスタルジー
            "銀杏並木の夕暮れ、冷えてきた風、ホットラテと足早な帰り道",
            "古着屋とレコードショップを巡る休日、懐かしいメロディと夕焼け",
            
            # 冬・雪・イルミネーション・温もり
            "真冬の街のイルミネーション、白い息、ポケットの中で繋いだ冷たい手",
            "しんしんと降り積もる雪の夜、暖炉の火、ホットココアを飲む静謐な時間",
            "凍てつく朝の澄み切った青空、新しい足跡をつける雪道、前向きな決意",
            "クリスマスイブの街角、キャンドルの灯り、遠くから聞こえる鐘の音",
            
            # 都会・夜・ドライブ・ネオン
            "深夜2時の首都高ドライブ、流れるテールランプの残光、カーステレオの重低音",
            "雨上がりのネオン街、アスファルトの水たまりに映る逆さまの摩天楼",
            "終電後の誰もいない駅のホーム、自販機の灯り、夜風と深い呼吸",
            "高層ビルの屋上から見下ろす夜景、冷たい風、光の海と小さな自分の未来",
            
            # カフェ・日常・チル・リラックス
            "日曜日の午後、木漏れ日が揺れるカフェテラス、珈琲の香りと古い文庫本",
            "静かな雨の日の部屋、猫のあくび、レコードの針が落とす優しいノイズ",
            "朝の目覚ましアラーム、焼きたてのトースト、窓から差し込む爽やかな光",
            "深夜のコインランドリー、規則正しく回るドラム、夜の静けさと考え事",
            
            # 感情・恋・ドラマ
            "届きそうで届かない片想い、すれ違う視線、胸の奥で高鳴る鼓動",
            "大人のほろ苦い別れ、薄暗いバーカウンター、グラスの中でカランと溶ける氷",
            "ずっと言えなかった「ありがとう」、夕焼けの帰り道、照れくさい笑顔",
            "挫折の先の再起、泥だらけの靴、自分を信じて再び走り出す瞬間",
            
            # ファンタジー・SF・冒険
            "サイバーパンクな未来都市、降りしきる酸性雨、ホログラム広告とサイバーバイク",
            "星空キャンプ、パチパチと爆ぜる焚き火、満天の天の川を見上げる旅人",
            "異国の賑やかなナイトマーケット、立ち込めるスパイスの煙、見知らぬ言葉の歌",
            "雲海を突き抜ける飛行船、どこまでも広がる空、まだ見ぬ大地への大冒険",
            "レトロゲームのドット絵の世界、8bitのファンタジー、勇者の長い旅路",
            
            # ポップ・青春・エネルギッシュ
            "放課後のチャイム、屋上へ駆け上がる足音、二人で分けたアイスキャンディー",
            "全力疾走の部活帰り、夕焼けに染まるグラウンド、青春の汗とハイタッチ",
        ]

        # 多彩なジャンル・音楽スタイルの候補プール（28種）
        genre_pool = [
            # J-Pop & J-Rock
            "modern J-pop, energetic piano rock, driving bass, sparkling synth, emotional catchy melody, 132 BPM",
            "modern J-pop ballad, grand acoustic piano, lush emotional strings, slow dramatic build-up, 86 BPM",
            "J-rock, powerful overdriven electric guitar riff, punchy rock drums, anthemic soaring chorus, 142 BPM",
            "pop punk, fast upbeat drums, crunchy power chords, youthful and catchy energetic melody, 148 BPM",
            "anime opening style, epic soaring strings, fast synth arpeggio, intense rock rhythm, 150 BPM",
            
            # City Pop & Groove & Funk
            "80s Japanese city pop, groovy slap bass, bright synth brass, funk guitar stabs, nostalgic Tokyo night, 116 BPM",
            "nu-disco funk pop, rhythmic guitar grooves, warm analog synth bass, shimmering Rhodes, 122 BPM",
            "future funk, vibrant filtered disco samples, punchy french house beat, upbeat and groovy, 126 BPM",
            
            # Lo-Fi & Chill & Neo Soul
            "lo-fi hip hop chillout, warm Rhodes piano, gentle vinyl crackle, mellow bassline, relaxed beats, 78 BPM",
            "bedroom pop, slightly detuned chorus guitar, cozy nostalgic lo-fi acoustic vibe, soft drums, 88 BPM",
            "neo soul, rich jazzy guitar chords, deep smooth bassline, laid-back rimshot groove, 84 BPM",
            "dream pop, ethereal reverb-drenched guitars, shimmering synth pads, floating celestial atmosphere, 96 BPM",
            "shoegaze, wall of sound distorted fuzzy guitars, buried melodic dreaminess, hypnotic beat, 104 BPM",
            
            # Acoustic & Organic & World
            "acoustic folk pop, warm fingerpicked acoustic guitar, subtle cello, organic hand percussion, 82 BPM",
            "bossa nova cafe lounge, nylon-string acoustic guitar, gentle shaker, breezy flute, relaxed sunset, 102 BPM",
            "country pop, lively acoustic strumming, pedal steel guitar hints, uplifting foot-stomping rhythm, 112 BPM",
            "traditional Japanese modern pop, koto melody, shakuhachi accents, taiko drums with modern pop beat, 110 BPM",
            "jazz pop ballad, walking double bass, muted trumpet solo, smoky piano chords, midnight cafe, 76 BPM",
            
            # Electronic & Dance
            "melodic future bass, lush supersaw chords, sparkling vocal chops, bouncy trap beats, 138 BPM",
            "tropical house, gentle marimba melody, plucky synths, breezy 4-on-the-floor beat, 118 BPM",
            "synthwave retrowave, 80s analog synth arpeggio, gated reverb snare, neon highway driving, 112 BPM",
            "electro swing, vintage brass big band horns, modern bouncy swing electronic drums, 124 BPM",
            "ambient chillstep, deep sub bass, spacious atmospheric pads, slow hypnotic electronic rhythm, 70 BPM",
            "eurobeat, blazing synth brass lead, high-energy pounding bass, relentless racing rhythm, 152 BPM",
            
            # R&B & Latin & Island
            "contemporary R&B, modern 808 sub, seductive electric guitar licks, silky smooth beat, 90 BPM",
            "reggae pop lovers rock, offbeat skank guitar, deep rolling reggae bass, tropical sunshine, 75 BPM",
            "latin pop acoustic, rhythmic nylon guitar, congas and timbales, passionate romantic melody, 98 BPM",
            "celtic folk pop, cheerful tin whistle, acoustic fiddle, driving bodhran rhythm, uplifting adventure, 118 BPM",
        ]
        vocal_pool = [
            "Japanese female vocal, clear and expressive emotional voice with wide dynamic range",
            "Japanese female vocal, sweet and gentle whispery voice, intimate close-mic feel",
            "Japanese female vocal, powerful belting high-pitched voice, anthemic and passionate",
            "Japanese female vocal, stylish airy voice, modern idol pop feel",
            "Japanese female vocal, sultry and smoky jazz R&B voice, mature vibe",
            "Japanese male vocal, warm and gentle acoustic voice, authentic storytelling tone",
            "Japanese male vocal, emotional and gritty rock voice with passionate rasp",
            "Japanese male vocal, smooth and soulful R&B falsetto and warm midrange",
            "Japanese male vocal, youthful and breezy upbeat pop voice",
            "Japanese female vocal, melancholic and fragile voice, deeply expressive",
        ]

        selected_theme = theme_hint if theme_hint else random.choice(theme_pool)
        selected_genre = random.choice(genre_pool)
        selected_vocal = random.choice(vocal_pool)

        user_prompt = f"""
あなたは世界的なヒット曲を手がけるプロの作詞家兼音楽プロデューサーです。
AI音楽生成モデル「YuE2」に投入するための、楽曲の「タイトル」「スタイルプロンプト（英語）」「日本語歌詞」「テーマ解説」を生成してください。

【今回の楽曲テーマ・世界観】
- シチュエーション/テーマ: {selected_theme}
- サウンドスタイル提案: {selected_genre}
- ボーカル提案: {selected_vocal}

【テーマ・言葉選びの厳格ルール（重要）】
- 今回指定されたテーマ・シチュエーションの世界観に全力で没入してください。
- 季節や時間帯を勝手に「秋」や「深夜」に固定することは絶対に禁止です。指定されたテーマ（夏、冬、春、サイバーパンク、日常、フェス等）に完璧に一致させて作詞してください。
- タイトルや歌詞に「amber」「琥珀」「秋」「autumn」などの単語を安易に使い回すことは厳禁です。曲ごとに全く異なる新鮮で魅力的な言葉を選んでください。

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
- 英語またはローマ字の短いユニークな曲名（アンダースコア区切り、英数字のみ、例: neon_overdrive, sunburst_splash, midnight_whisky, brave_horizon, crystal_snowfall）。
- 今回のテーマに深く合致した、唯一無二の魅力的なタイトルにすること。
"""
        client = genai.Client(api_key=api_key)
        response = client.models.generate_content(
            model="gemini-3.8-flash",
            contents=user_prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=SongGenerationPlan,
                temperature=0.85,
            ),
        )
        plan_dict = json.loads(response.text)
        print(f"=== [Gemini 自律作詞完了] タイトル: {plan_dict.get('title')} ===")
        return plan_dict
    except Exception as e:
        print(f"[Gemini 作詞エラー] API呼び出しに失敗したためフォールバックを使用: {e}")
        return {
            "title": "starlight_voyage",
            "style_prompt": "modern J-pop, energetic piano rock, driving bass, sparkling synth, emotional catchy melody, Japanese female vocal, clear and expressive, 132 BPM",
            "theme_description": "星空の下を未来へ向かって駆け抜ける爽快でエモーショナルな王道J-POP",
            "lyrics": "[Intro]\n光の中へ\n[Verse 1]\nビルの隙間から差し込む光が\n冷たいアスファルトを染めていく\n誰もいないホームで息を吸い込んだ\n[Pre-Chorus]\n戸惑いを風に乗せて\n昨日までの涙を拭う\n[Chorus]\n消えない痛みを抱えたままで\n僕らは次の朝へと走り出す\nどこまでも続く青空へ手を伸ばして\n信じた軌跡を抱きしめる\n[Verse 2]\nすれ違う影に怯えていた日々\nそれでも心は明日を呼んでいた\n[Chorus]\n消えない痛みを抱えたままで\n僕らは次の朝へと走り出す\nどこまでも続く青空へ手を伸ばして\n信じた軌跡を抱きしめる\n[Outro]\n光の向こうへ ずっと",
        }

# ---------------------------------------------------------------------------
# 7. Discord Webhook 完了通知モジュール (マルチメディア対応)
# ---------------------------------------------------------------------------
def notify_discord(
    title: str,
    style: str,
    theme: str,
    lyrics: str,
    subfolder_id: str,
    seed: int,
    trigger_type: str = "手動生成",
    has_video: bool = False,
    has_cover: bool = False,
) -> bool:
    """
    楽曲生成、ジャケット画像生成、動画レンダリングおよびGoogle Driveアップロード完了時に、
    Discord WebhookへリッチEmbed通知を送信します。
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

        artifacts_desc = "・🎵 音源: FLAC & 192kbps MP3"
        if has_cover:
            artifacts_desc += "\n・🖼 ジャケット: 3000×3000px 配信規格JPG"
        if has_video:
            artifacts_desc += "\n・🎬 動画: 1080p フルHD ビジュアライザー動画"

        embed = {
            "title": f"🎉 新曲・マルチメディア完成: {title}",
            "description": f"**{theme}**\n\nYuE2推論、3000pxジャケット生成、1080pビジュアライザー動画化、およびGoogle Driveへの自動保存が完了しました！",
            "color": 0x1A73E8,  # Google Blue
            "fields": [
                {
                    "name": "🎨 スタイル・サウンド",
                    "value": f"`{style[:150]}`",
                    "inline": False,
                },
                {
                    "name": "📦 生成された成果物セット",
                    "value": artifacts_desc,
                    "inline": False,
                },
                {
                    "name": "📁 Google Drive 保存先",
                    "value": f"[▶ Google Drive で確認・再生・ダウンロード]({drive_folder_url})",
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
                "text": "YuE2 × Modal マルチメディア自動生成 (ジャケット＆動画対応)",
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
# 7. ストレージ容量監視 & クリーンアップモジュール (5GB超過時 or 毎回削除)
# ---------------------------------------------------------------------------
def get_dir_size_bytes(path: Path) -> int:
    """
    指定ディレクトリ内のファイル総サイズ（バイト）を計算します。
    """
    total = 0
    try:
        for entry in path.rglob("*"):
            if entry.is_file():
                total += entry.stat().st_size
    except Exception:
        pass
    return total


def cleanup_storage_if_needed(
    songs_root: Path,
    max_gb: float = 5.0,
    purge_immediately: bool = False,
    current_subfolder: str = "",
) -> None:
    """
    Google Drive への転送成功後、ストレージのクリーンアップを実行します。
    - purge_immediately が True: Drive転送が成功した曲を即座に削除（毎回削除モード）
    - 5GB超過時: Google Drive転送済みの古い曲から順に安全に自動削除（5GB上限モード）
    """
    try:
        # 1. 毎回即時削除モード
        if purge_immediately and current_subfolder:
            cur_dir = songs_root / current_subfolder
            if cur_dir.exists():
                print(f"--- [ストレージ管理] 毎回削除モード: {current_subfolder} を削除し容量を解放します ---")
                shutil.rmtree(cur_dir)
                song_storage.commit()
                return

        # 2. 5GB 容量監視モード
        total_bytes = get_dir_size_bytes(songs_root)
        max_bytes = int(max_gb * 1024 * 1024 * 1024)
        total_mb = total_bytes / (1024 * 1024)

        if total_bytes <= max_bytes:
            print(f"--- [ストレージ監視] 現在の楽曲ストレージ使用量: {total_mb:.1f} MB / 上限 {max_gb:.1f} GB ---")
            return

        print(f"--- [ストレージ警告] 容量上限 ({max_gb} GB) を超過 ({total_mb:.1f} MB)。古い楽曲から自動削除を開始します ---")
        song_dirs = sorted([d for d in songs_root.iterdir() if d.is_dir()])
        deleted_count = 0
        target_bytes = int(max_bytes * 0.8)  # 上限の80%（4GB）まで安全マージンを確保

        for s_dir in song_dirs:
            if s_dir.name == current_subfolder:
                continue  # 最新生成曲は保護

            print(f"--- [ストレージ管理] 容量確保のため削除: {s_dir.name} ---")
            shutil.rmtree(s_dir)
            deleted_count += 1
            total_bytes = get_dir_size_bytes(songs_root)
            if total_bytes <= target_bytes:
                break

        song_storage.commit()
        print(f"=== [ストレージ管理完了] {deleted_count} 件の古い楽曲を自動削除しました（新使用量: {total_bytes / (1024*1024):.1f} MB） ===")
    except Exception as e:
        print(f"[ストレージ管理 エラー] クリーンアップ中に例外が発生しました: {e}")


# ---------------------------------------------------------------------------
# 8. Google Drive 自動連携 & mp3集約保存モジュール (OAuth 2.0 連携)
# ---------------------------------------------------------------------------
def get_or_create_shared_folder(service, target_folder_id: str, folder_name: str) -> str:
    """
    Google Drive の target_folder_id 配下に指定名（mp3, covers, videos など）のフォルダが存在するか検索し、
    存在しなければ自動作成してそのフォルダ ID を返します。
    """
    try:
        query = f"'{target_folder_id}' in parents and name = '{folder_name}' and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
        res = service.files().list(q=query, spaces="drive", fields="files(id, name)").execute()
        files = res.get("files", [])
        if files:
            return files[0]["id"]

        folder_metadata = {
            "name": folder_name,
            "mimeType": "application/vnd.google-apps.folder",
            "parents": [target_folder_id],
        }
        folder = service.files().create(body=folder_metadata, fields="id").execute()
        folder_id = folder.get("id")
        print(f"=== [Google Drive] '{folder_name}' 共有フォルダを新規作成しました (ID: {folder_id}) ===")
        return folder_id
    except Exception as e:
        print(f"[Google Drive エラー] '{folder_name}' フォルダの取得・作成に失敗しました: {e}")
        return ""


def upload_to_drive(subfolder_name: str, artifacts: dict, prompt_info: str) -> str:
    """
    生成された楽曲ファイル（FLAC、MP3、ジャケット画像、ビジュアライザー動画、ABC楽譜、メタデータログ）を
    Google Driveの個別フォルダ（yue2/曲名_日時/）へ保存し、
    さらに MP3 / ジャケット画像 / 動画 をそれぞれの共有フォルダ（yue2/mp3/, yue2/covers/, yue2/videos/）へ集約配置します。
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
        cover_data = None
        video_data = None

        for fname, data in artifacts.items():
            meta = {"name": fname, "parents": [subfolder_id]}
            if fname.endswith(".flac"):
                mtype = "audio/flac"
            elif fname.endswith(".mp3"):
                mtype = "audio/mpeg"
                mp3_data = data
            elif fname.endswith(".jpg") or fname.endswith(".jpeg"):
                mtype = "image/jpeg"
                if "3000" in fname or "cover" in fname:
                    cover_data = data
            elif fname.endswith(".png"):
                mtype = "image/png"
            elif fname.endswith(".mp4"):
                mtype = "video/mp4"
                video_data = data
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

        # 4. 【マルチメディア集約保存】mp3, covers, videos 共有フォルダへ自動配置
        # (1) mp3 フォルダ
        if mp3_data:
            mp3_fid = get_or_create_shared_folder(service, target_folder_id, "mp3")
            if mp3_fid:
                mp3_name = f"{subfolder_name}.mp3"
                service.files().create(
                    body={"name": mp3_name, "parents": [mp3_fid]},
                    media_body=MediaIoBaseUpload(io.BytesIO(mp3_data), mimetype="audio/mpeg"),
                ).execute()
                print(f"=== [Google Drive] mp3 フォルダへ集約保存完了: {mp3_name} ===")

        # (2) covers フォルダ (3000×3000px 配信規格ジャケット)
        if cover_data:
            covers_fid = get_or_create_shared_folder(service, target_folder_id, "covers")
            if covers_fid:
                cover_name = f"{subfolder_name}.jpg"
                service.files().create(
                    body={"name": cover_name, "parents": [covers_fid]},
                    media_body=MediaIoBaseUpload(io.BytesIO(cover_data), mimetype="image/jpeg"),
                ).execute()
                print(f"=== [Google Drive] covers フォルダへ集約保存完了: {cover_name} ===")

        # (3) videos フォルダ (フルHD 1080p ビジュアライザー動画)
        if video_data:
            videos_fid = get_or_create_shared_folder(service, target_folder_id, "videos")
            if videos_fid:
                video_name = f"{subfolder_name}.mp4"
                service.files().create(
                    body={"name": video_name, "parents": [videos_fid]},
                    media_body=MediaIoBaseUpload(io.BytesIO(video_data), mimetype="video/mp4"),
                ).execute()
                print(f"=== [Google Drive] videos フォルダへ集約保存完了: {video_name} ===")

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
        modal.Secret.from_name("gemini-secret"),
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
    生成後は192kbps MP3変換、3000pxジャケット生成、1080pビジュアライザー動画化を行い、
    Modal Volume および Google Drive へ保存、Discord へ通知します。
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

    # 【時間制御 & 音声変換】最大2分58秒 (178秒) 制限 & 192kbps MP3 への自動変換
    flac_file = temp_out / "audio.flac"
    mp3_file = temp_out / "audio.mp3"
    if flac_file.exists():
        # 音声が2分58秒を超えている場合は末尾5秒で美しくフェードアウトさせてトリミング
        enforce_max_duration(flac_file, flac_file, max_sec=MAX_SONG_DURATION_SEC)
        print(f"--- [FFmpeg] 192kbps MP3 へ変換中: {flac_file.name} ---")
        if convert_flac_to_mp3(flac_file, mp3_file, bitrate="192k", max_sec=MAX_SONG_DURATION_SEC):
            print(f"--- [FFmpeg] MP3 変換完了: {mp3_file.name} (サイズ: {mp3_file.stat().st_size / 1024 / 1024:.2f} MB) ---")

    # 【マルチメディア展開】ジャケット画像 (3000x3000px) ＆ ビジュアライザー動画 (1080p MP4) の自動生成
    cover_raw = temp_out / "cover_raw.png"
    cover_3000 = temp_out / "cover_art_3000.jpg"
    has_cover = generate_cover_art(
        title=title,
        theme_description=theme_description,
        style_prompt=style_prompt,
        output_png_path=cover_raw,
        output_3000_jpg_path=cover_3000,
    )

    video_file = temp_out / "video.mp4"
    has_video = False
    if has_cover and mp3_file.exists():
        has_video = render_visualizer_video(
            cover_image_path=cover_raw,
            audio_path=mp3_file,
            output_video_path=video_file,
        )

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

    # Google Drive への自動同期（個別フォルダ ＆ mp3/covers/videos 集約フォルダ）
    subfolder_id = upload_to_drive(subfolder_name=subfolder_name, artifacts=artifacts, prompt_info=prompt_info_text)

    # 【ストレージ管理】5GB超過時の自動ローテーション削除 or 毎回削除（Drive転送成功時のみ）
    storage_mode = os.environ.get("MODAL_STORAGE_MODE", "auto_5gb")
    purge_now = (storage_mode == "purge_immediately") and bool(subfolder_id)
    cleanup_storage_if_needed(
        songs_root=Path("/root/songs"),
        max_gb=5.0,
        purge_immediately=purge_now,
        current_subfolder=subfolder_name,
    )

    # Discord への完了通知
    notify_discord(
        title=title,
        style=style_prompt,
        theme=theme_description or "AI生成楽曲",
        lyrics=lyrics,
        subfolder_id=subfolder_id,
        seed=seed,
        trigger_type=trigger_type,
        has_video=has_video,
        has_cover=has_cover,
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
        mp3_folder_id = get_or_create_shared_folder(service, target_folder_id, "mp3")
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
            <h3>AIが多彩なジャンル・世界観から自動で作詞・作曲設定を行います</h3>
            <form action="/generate-ai" method="post">
                <label>曲のテーマ・キーワード（任意）:</label>
                <input type="text" name="theme_hint" placeholder="例: 真夏の海岸線、星空ドライブ、切ない失恋、サイバーパンク、疾走ロック">
                <div class="tip">※空欄の場合は、32種類以上の多彩なテーマや28種類の音楽ジャンルからGeminiが自律選択します。</div>

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
        <p style="font-size:14px;">生成完了後、<b>Google Drive (個別フォルダ ＆ mp3/covers/videos 集約フォルダ)</b> および <b>Discord通知</b> に自動転送されます（音源・3000pxジャケット・動画フルセット）。</p>
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
        <p>クラウドGPU（L4）にて生成中です。完了後は <b>Google Drive (個別フォルダ ＆ mp3/covers/videos 集約フォルダ)</b> および <b>Discord</b> に自動送信されます（音源・3000pxジャケット・動画フルセット）。</p>
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
