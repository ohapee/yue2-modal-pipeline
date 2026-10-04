import modal
import datetime
from pathlib import Path

app = modal.App("yue2-song-generator")
model_volume = modal.Volume.from_name("yue2-model-cache", create_if_missing=True)

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
        "git+https://github.com/multimodal-art-projection/YuE.git",
    )
    .env({"HF_HOME": "/root/models/hf"})
)

@app.function(
    image=image,
    gpu="L4",
    timeout=600,
    volumes={"/root/models": model_volume},
)
def generate_music(style_prompt: str, lyrics: str, seed: int = 42) -> dict:
    import shutil
    from yue2 import YuE2Pipeline

    temp_out = Path("/tmp/yue2_output")
    if temp_out.exists():
        shutil.rmtree(temp_out)
    temp_out.mkdir(parents=True, exist_ok=True)

    print(f"--- YuE2パイプライン初期化 (Seed: {seed}) ---")
    with YuE2Pipeline.from_pretrained("m-a-p/YuE2-3B", device="cuda") as pipe:
        print("--- 楽曲生成開始 ---")
        song = pipe(style=style_prompt, lyrics=lyrics, cot="full", seed=seed)
        song.save_artifacts(str(temp_out))

    artifacts = {}
    for f in temp_out.iterdir():
        if f.is_file():
            artifacts[f.name] = f.read_bytes()

    print("--- クラウド生成完了 ---")
    return artifacts

@app.local_entrypoint()
def main(
    style: str = "Japanese, clear expressive female vocal, modern J-pop ballad, 92 BPM, acoustic piano, strings quartet, warm bass",
    lyrics_file: str = "",
    title: str = "demo_song",
    seed: int = 42,
):
    # 歌詞ファイルの読み込み（未指定時はデフォルト歌詞を使用）
    if lyrics_file and Path(lyrics_file).exists():
        lyrics = Path(lyrics_file).read_text(encoding="utf-8").strip()
        print(f"歌詞ファイルを読み込みました: {lyrics_file}")
    else:
        lyrics = """[Verse]
ビルの隙間から差し込む光が
冷たいアスファルトを染めていく
[Chorus]
消えない痛みを抱えたままで
僕らは次の朝へと走り出す
誰も見たことのない空の向こうへ
[Outro]
光の中へ"""
        if lyrics_file:
            print(f"警告: '{lyrics_file}' が見つからないため、デフォルト歌詞を使用します。")

    print(f"\n==========================================")
    print(f"Title : {title}")
    print(f"Seed  : {seed}")
    print(f"Style : {style}")
    print(f"==========================================\n")

    print("クラウドGPU（L4）へジョブを投入中...")
    artifacts = generate_music.remote(style, lyrics, seed=seed)

    # 上書き防止のため、曲名とタイムスタンプ付きのフォルダを作成
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = Path("outputs") / f"{title}_{timestamp}"
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"\nローカルの '{out_dir}/' へ保存中...")
    for filename, data in artifacts.items():
        dest = out_dir / filename
        dest.write_bytes(data)
        print(f" - {filename}")

    # 次回の比較・振り返り用にプロンプト情報をテキスト保存
    info_file = out_dir / "prompt_info.txt"
    info_file.write_text(
        f"Title: {title}\nSeed: {seed}\nStyle: {style}\n\nLyrics:\n{lyrics}\n",
        encoding="utf-8"
    )
    print(f" - prompt_info.txt (設定ログ)")

    print(f"\n🎉 完了しました！ 保存先: {out_dir}/")
