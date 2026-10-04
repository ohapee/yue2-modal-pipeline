import modal
import datetime
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
        "git+https://github.com/multimodal-art-projection/YuE.git",
    )
    .env({"HF_HOME": "/root/models/hf"})
)

@app.function(
    image=image,
    gpu="L4",
    timeout=600,
    volumes={"/root/models": model_volume, "/root/songs": song_storage},
)
def generate_music_core(style_prompt: str, lyrics: str, seed: int = 42, title: str = "song") -> dict:
    import shutil
    from yue2 import YuE2Pipeline

    temp_out = Path("/tmp/yue2_output")
    if temp_out.exists():
        shutil.rmtree(temp_out)
    temp_out.mkdir(parents=True, exist_ok=True)

    print(f"--- YuE2 生成開始: {title} (Seed: {seed}) ---")
    with YuE2Pipeline.from_pretrained("m-a-p/YuE2-3B", device="cuda") as pipe:
        song = pipe(style=style_prompt, lyrics=lyrics, cot="full", seed=seed)
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
        f"Title: {title}\nSeed: {seed}\nStyle: {style_prompt}\n\nLyrics:\n{lyrics}\n",
        encoding="utf-8"
    )
    song_storage.commit()
    print(f"--- クラウドVolumeへ永続保存完了: {permanent_dir} ---")
    return artifacts

@app.function(schedule=modal.Cron("0 0 * * 1"))
def scheduled_batch_generation():
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

web_app = FastAPI()

@web_app.get("/", response_class=HTMLResponse)
async def web_ui():
    return """
    <!DOCTYPE html>
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
        </style>
    </head>
    <body>
        <h2>🎵 YuE2 クラウド楽曲生成</h2>
        <form action="/generate" method="post">
            <label>曲名 (Title):</label>
            <input type="text" name="title" value="mobile_track" required>
            <label>スタイルプロンプト (Style):</label>
            <input type="text" name="style" value="Japanese, modern J-pop ballad, 92 BPM, piano, strings" required>
            <label>シード値 (Seed):</label>
            <input type="number" name="seed" value="42">
            <label>歌詞 (Lyrics):</label>
            <textarea name="lyrics" rows="8">[Verse]\n差し込む光の中で\n歩き始める街\n[Chorus]\n明日へと続く道\n諦めない心で\n[Outro]\n未来へ</textarea>
            <button type="submit">クラウドGPUで生成を開始</button>
        </form>
    </body>
    </html>
    """

@web_app.post("/generate", response_class=HTMLResponse)
async def handle_generate(request: Request):
    form = await request.form()
    title = str(form.get("title", "web_track"))
    style = str(form.get("style", ""))
    lyrics = str(form.get("lyrics", ""))
    seed = int(form.get("seed", 42))

    generate_music_core.spawn(style, lyrics, seed=seed, title=title)
    return f"""
    <!DOCTYPE html>
    <html><body style="font-family:sans-serif; text-align:center; padding:50px;">
        <h3 style="color:#1a73e8;">🚀 生成リクエストを受け付けました</h3>
        <p>Title: <b>{title}</b></p>
        <p>クラウドGPU（L4）にて生成中です。完了した音声は自動的にクラウドストレージに保管されます。</p>
        <p><a href="/">← 戻る</a></p>
    </body></html>
    """

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
        lyrics = "[Verse]\n光の中へ歩き出す\n[Chorus]\n未来を信じて\n[Outro]\n遠くへ"

    print(f"ローカルからクラウドGPUへジョブを投入: {title} (Seed: {seed})")
    artifacts = generate_music_core.remote(style, lyrics, seed=seed, title=title)
    
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = Path("outputs") / f"{title}_{timestamp}"
    out_dir.mkdir(parents=True, exist_ok=True)
    for fname, data in artifacts.items():
        (out_dir / fname).write_bytes(data)
    print(f"ローカル保存完了: {out_dir}/")
