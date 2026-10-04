import modal
import datetime
import re
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse

app = modal.App("yue2-song-generator")
model_volume = modal.Volume.from_name("yue2-model-cache", create_if_missing=True)
song_storage = modal.Volume.from_name("yue2-generated-songs", create_if_missing=True)

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
        "git+https://github.com/multimodal-art-projection/YuE.git",
    )
    .env({"HF_HOME": "/root/models/hf"})
)

def optimize_lyrics(raw_lyrics: str, max_line_len: int = 8) -> str:
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
        if line.startswith("[") and line.endswith("]"):
            optimized_lines.append(line)
            continue

        if has_kakasi:
            conversion = kks.convert(line)
            tokens = []
            for item in conversion:
                hira = item.get("hira", "")
                hira_clean = re.sub(r"[、。，．！？!?\s]", "", hira)
                if hira_clean:
                    tokens.append(hira_clean)

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
            for i in range(0, len(line), max_line_len):
                optimized_lines.append(line[i:i + max_line_len])

    return "\n".join(optimized_lines)

@app.function(
    image=image,
    gpu="L4",
    timeout=600,
    volumes={"/root/models": model_volume, "/root/songs": song_storage},
)
def generate_music_core(style_prompt: str, lyrics: str, seed: int = 42, title: str = "song") -> dict:
    import shutil
    from yue2 import YuE2Pipeline

    optimized_lyrics = optimize_lyrics(lyrics)
    print(f"\n=== Optimized Lyrics ===\n{optimized_lyrics}\n========================\n")

    temp_out = Path("/tmp/yue2_output")
    if temp_out.exists():
        shutil.rmtree(temp_out)
    temp_out.mkdir(parents=True, exist_ok=True)

    print(f"--- YuE2 Start: {title} (Seed: {seed}) ---")
    with YuE2Pipeline.from_pretrained("m-a-p/YuE2-3B", device="cuda") as pipe:
        song = pipe(style=style_prompt, lyrics=optimized_lyrics, cot="full", seed=seed)
        song.save_artifacts(str(temp_out))

    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    permanent_dir = Path("/root/songs") / f"{title}_{timestamp}"
    permanent_dir.mkdir(parents=True, exist_ok=True)

    artifacts = {}
    for f in temp_out.iterdir():
        if f.is_file():
            data = f.read_bytes()
            artifacts[f.name] = data
            (permanent_dir / f.name).write_bytes(data)

    (permanent_dir / "prompt_info.txt").write_text(
        f"Title: {title}\nSeed: {seed}\nStyle: {style_prompt}\n\nOriginal Lyrics:\n{lyrics}\n\nOptimized Lyrics:\n{optimized_lyrics}\n",
        encoding="utf-8"
    )
    song_storage.commit()
    print(f"--- Saved to Volume: {permanent_dir} ---")
    return artifacts

@app.function(schedule=modal.Cron("0 0 * * 1"))
def scheduled_batch_generation():
    default_style = "Japanese, chill acoustic guitar, lo-fi hip hop beats, 80 BPM, warm piano, nostalgic mood"
    default_lyrics = "[Verse]\n穏やかな朝\n新しい日\n[Chorus]\n歩き出そう\n空へ\n[Outro]\n風の中"
    generate_music_core.remote(
        style_prompt=default_style,
        lyrics=default_lyrics,
        seed=int(datetime.datetime.now().timestamp()) % 10000,
        title="scheduled_lofi"
    )

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
    <h2>YuE2 Cloud Music Generator</h2>
    <form action="/generate" method="post">
        <label>Title:</label>
        <input type="text" name="title" value="auto_song" required>
        <label>Style:</label>
        <input type="text" name="style" value="Japanese, modern J-pop ballad, 92 BPM, piano, strings" required>
        <label>Seed:</label>
        <input type="number" name="seed" value="42">
        <label>Lyrics:</label>
        <textarea name="lyrics" rows="8">[Verse]
ビルの隙間から差し込む光が
冷たいアスファルトを染めていく
[Chorus]
消えない痛みを抱えたままで
僕らは次の朝へと走り出す
[Outro]
光の中へ</textarea>
        <div class="tip">※入力された歌詞は自動的に「ひらがな・分かち書き・5〜8文字改行」に最適化されます。</div>
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
    <h3 style="color:#1a73e8;">生成リクエストを受け付けました</h3>
    <p>Title: <b>{title}</b></p>
    <p>入力された歌詞は自動的に<b>「ひらがな・分かち書き・5〜8文字改行」</b>へ最適化されて推論されます。</p>
    <p>クラウドGPU（L4）にて生成中です。完了した音声は自動的にクラウドストレージ（Volume: yue2-generated-songs）に保管されます。</p>
    <p style="margin-top:20px;"><a href="/">← もう1曲生成する</a></p>
</body></html>"""

@app.function(image=image)
@modal.asgi_app()
def web():
    return web_app

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
