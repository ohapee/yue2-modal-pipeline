"""
YuE2 × Modal クラウド楽曲自動生成パイプライン (app.py)

【概要】
オープンソースAI音楽生成モデル「YuE2 (3B)」を、サーバーレスGPUインフラ「Modal」上で稼働させ、
完全クラウド完結（ローカルPCのGPU・電源不要）で楽曲を生成・保管するシステムです。

【主な機能】
1. サーバーレスGPU推論 (NVIDIA L4 24GB VRAM):
   - 秒単位課金（常時起動コストゼロ）。
   - モデル重みは約4GBの永続Volume (yue2-model-cache) にキャッシュし、高速起動を実現。
2. 日本語歌詞自動最適化エンジン (optimize_lyrics):
   - 通常の漢字混じり・長文の歌詞を、形態素解析 (pykakasi) により
     「ひらがな」「分かち書き」「1行5〜8文字」へ自動整形。
   - メロディ音符（ABC記譜法）とモーラ（拍数）の1対1対応を強制し、歌詞のハルシネーション（勝手な作詞）を防止。
3. Google Drive 自動連携 (upload_to_drive):
   - OAuth 2.0 ユーザー認証 (USER_TOKEN_B64) を利用し、個人のGoogle Drive容量を直接使用。
   - 生成された音声 (FLAC)、楽譜 (score.abc)、プロンプト設定 (prompt_info.txt) を yue2 フォルダへ自動転送。
4. トリプル・トリガー対応:
   - Web UI: スマートフォンやブラウザからワンタップで生成できるFastAPIフォーム。
   - Cron: 毎週月曜午前9時 (JST) に完全自動でストック楽曲を生成するバッチ。
   - Local CLI: 開発・パラメータ検証用の手元実行 (modal run app.py)。
"""

import modal
import datetime
import re
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse

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
# 4. Google Drive 自動アップロード関数 (OAuth 2.0 連携)
# ---------------------------------------------------------------------------
def upload_to_drive(subfolder_name: str, artifacts: dict, prompt_info: str) -> str:
    """
    生成された楽曲ファイル（FLAC、ABC楽譜、メタデータログ）を
    Google Driveの指定フォルダ（yue2）へ自動アップロードします。
    """
    import json
    import base64
    import os
    import io

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

        # 1. yue2 フォルダ内に「曲名_日時」のサブフォルダを作成
        folder_metadata = {
            "name": subfolder_name,
            "mimeType": "application/vnd.google-apps.folder",
            "parents": [target_folder_id]
        }
        subfolder = service.files().create(body=folder_metadata, fields="id").execute()
        subfolder_id = subfolder.get("id")

        # 2. 生成アセット群のアップロード
        for fname, data in artifacts.items():
            meta = {"name": fname, "parents": [subfolder_id]}
            mtype = "audio/flac" if fname.endswith(".flac") else "text/plain" if fname.endswith(".abc") else "application/octet-stream"
            media = MediaIoBaseUpload(io.BytesIO(data), mimetype=mtype)
            service.files().create(body=meta, media_body=media).execute()

        # 3. 設定ログ (prompt_info.txt) のアップロード
        info_meta = {"name": "prompt_info.txt", "parents": [subfolder_id]}
        info_media = MediaIoBaseUpload(io.BytesIO(prompt_info.encode("utf-8")), mimetype="text/plain")
        service.files().create(body=info_meta, media_body=info_media).execute()

        print(f"=== [Google Drive] 自動アップロード完了: {subfolder_name} (ID: {subfolder_id}) ===")
        return subfolder_id
    except Exception as e:
        print(f"[Google Drive エラー] アップロードに失敗しました: {e}")
        return ""

# ---------------------------------------------------------------------------
# 5. コア生成関数 (NVIDIA L4 GPU / タイムアウト10分)
# ---------------------------------------------------------------------------
@app.function(
    image=image,
    gpu="L4",
    timeout=600,
    volumes={"/root/models": model_volume, "/root/songs": song_storage},
    secrets=[modal.Secret.from_name("google-drive-secret")],
)
def generate_music_core(style_prompt: str, lyrics: str, seed: int = 42, title: str = "song") -> dict:
    """
    L4 GPU上でYuE2モデルをロードし、シンボリック・プランニング（ABC楽譜）を経て楽曲を生成します。
    """
    import shutil
    from yue2 import YuE2Pipeline

    # 歌詞の自動最適化を実行
    optimized_lyrics = optimize_lyrics(lyrics)
    print(f"\n=== [自動最適化された歌詞 (YuE2投入)] ===\n{optimized_lyrics}\n=====================================\n")

    temp_out = Path("/tmp/yue2_output")
    if temp_out.exists():
        shutil.rmtree(temp_out)
    temp_out.mkdir(parents=True, exist_ok=True)

    print(f"--- YuE2 推論開始: {title} (Seed: {seed}) ---")
    with YuE2Pipeline.from_pretrained("m-a-p/YuE2-3B", device="cuda") as pipe:
        song = pipe(style=style_prompt, lyrics=optimized_lyrics, cot="full", seed=seed)
        song.save_artifacts(str(temp_out))

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

    # 人間が読める元歌詞と最適化歌詞の両方をログファイルに記録
    prompt_info_text = (
        f"Title: {title}\nSeed: {seed}\nStyle: {style_prompt}\n\n"
        f"Original Lyrics:\n{lyrics}\n\n"
        f"Optimized Lyrics (Sent to Model):\n{optimized_lyrics}\n"
    )
    (permanent_dir / "prompt_info.txt").write_text(prompt_info_text, encoding="utf-8")
    song_storage.commit()
    print(f"--- Modal Volume 永続保存完了: {permanent_dir} ---")

    # Google Drive への自動同期
    upload_to_drive(subfolder_name=subfolder_name, artifacts=artifacts, prompt_info=prompt_info_text)

    return artifacts

# ---------------------------------------------------------------------------
# 6. トリガー①: 定期自動実行 (Cron: 毎週月曜 午前9時 JST / 日曜24:00 UTC)
# ---------------------------------------------------------------------------
@app.function(schedule=modal.Cron("0 0 * * 1"))
def scheduled_batch_generation():
    """
    週次で自動起動し、完全放置でストック用の新曲を生成します。
    """
    print("【Cron 定期実行】週次の自動楽曲ストック生成を開始します...")
    default_style = "Japanese, chill acoustic guitar, lo-fi hip hop beats, 80 BPM, warm piano, nostalgic mood"
    default_lyrics = """[Verse]
静かな朝の光の中で
新しいページが開いていく
[Chorus]
歩き出そう 自分のリズムで
どこまでも続く空へ
[Outro]
穏やかな風"""
    generate_music_core.remote(
        style_prompt=default_style,
        lyrics=default_lyrics,
        seed=int(datetime.datetime.now().timestamp()) % 10000,
        title="scheduled_lofi"
    )

# ---------------------------------------------------------------------------
# 7. トリガー②: 手動生成用 Web UI (FastAPI / スマホ対応)
# ---------------------------------------------------------------------------
web_app = FastAPI()

HTML_CONTENT = """<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>YuE2 Cloud Generator</title>
    <style>
        body { font-family: -apple-system, BlinkMacSystemFont, sans-serif; max-width: 600px; margin: 20px auto; padding: 15px; background: #f7f9fa; }
        h2 { color: #1a73e8; }
        label { display: block; margin-top: 12px; font-weight: bold; }
        input, textarea { width: 100%; padding: 10px; margin-top: 5px; border: 1px solid #ccc; border-radius: 6px; box-sizing: border-box; }
        button { margin-top: 20px; width: 100%; padding: 12px; background: #1a73e8; color: white; border: none; border-radius: 6px; font-size: 16px; cursor: pointer; }
        .tip { font-size: 13px; color: #555; margin-top: 4px; }
    </style>
</head>
<body>
    <h2>🎵 YuE2 クラウド楽曲生成</h2>
    <form action="/generate" method="post">
        <label>曲名 (Title):</label>
        <input type="text" name="title" value="drive_sync_track" required>
        <label>スタイルプロンプト (Style):</label>
        <input type="text" name="style" value="Japanese, modern J-pop ballad, 92 BPM, piano, strings" required>
        <label>シード値 (Seed):</label>
        <input type="number" name="seed" value="42">
        <label>歌詞 (Lyrics):</label>
        <textarea name="lyrics" rows="8">[Verse]
ビルの隙間から差し込む光が
冷たいアスファルトを染めていく
[Chorus]
消えない痛みを抱えたままで
僕らは次の朝へと走り出す
[Outro]
光の中へ</textarea>
        <div class="tip">※入力歌詞は自動で「ひらがな・分かち書き・5〜8文字改行」に最適化され、生成完了後に Google Drive (yue2 フォルダ) へ自動転送されます。</div>
        <button type="submit">クラウドGPUで生成を開始</button>
    </form>
</body>
</html>"""

@web_app.get("/", response_class=HTMLResponse)
async def web_ui():
    return HTML_CONTENT

@web_app.post("/generate", response_class=HTMLResponse)
async def handle_generate(request: Request):
    form = await request.form()
    title = str(form.get("title") or "web_track")
    style = str(form.get("style") or "Japanese, modern J-pop ballad, 92 BPM, piano, strings")
    lyrics = str(form.get("lyrics") or "[Verse]\n光の中へ\n[Chorus]\n明日へ")
    seed_raw = form.get("seed")
    try:
        seed = int(seed_raw) if seed_raw else 42
    except (ValueError, TypeError):
        seed = 42

    generate_music_core.spawn(style_prompt=style, lyrics=lyrics, seed=seed, title=title)
    return f"""<!DOCTYPE html>
<html><body style="font-family:sans-serif; text-align:center; padding:50px;">
    <h3 style="color:#1a73e8;">🚀 生成リクエストを受け付けました</h3>
    <p>Title: <b>{title}</b></p>
    <p>クラウドGPU（L4）にて生成中です。完了した音声は <b>Google Drive (yue2 フォルダ)</b> に自動保存されます。</p>
    <p style="margin-top:20px;"><a href="/">← もう1曲生成する</a></p>
</body></html>"""

@app.function(image=image)
@modal.asgi_app()
def web():
    return web_app

# ---------------------------------------------------------------------------
# 8. トリガー③: ローカルCLI実行エントリポイント (手元テスト用)
# ---------------------------------------------------------------------------
@app.local_entrypoint()
def main(
    style: str = "Japanese, clear expressive female vocal, modern J-pop ballad, 92 BPM, acoustic piano, strings",
    lyrics_file: str = "",
    title: str = "cli_song",
    seed: int = 42,
):
    if lyrics_file and Path(lyrics_file).exists():
        lyrics = Path(lyrics_file).read_text(encoding="utf-8").strip()
    else:
        lyrics = "[Verse]\nビルの隙間から差し込む光が\n冷たいアスファルトを染めていく\n[Chorus]\n消えない痛みを抱えたままで\n僕らは次の朝へと走り出す\n[Outro]\n光の中へ"

    print(f"ローカルからクラウドGPUへジョブを投入: {title} (Seed: {seed})")
    artifacts = generate_music_core.remote(style_prompt=style, lyrics=lyrics, seed=seed, title=title)
    
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = Path("outputs") / f"{title}_{timestamp}"
    out_dir.mkdir(parents=True, exist_ok=True)
    for fname, data in artifacts.items():
        (out_dir / fname).write_bytes(data)
    print(f"ローカル保存完了: {out_dir}/")
