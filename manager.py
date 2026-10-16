"""
YuE2 Google Drive 成果物管理マネージャー (manager.py)
Google Drive 上に保存された楽曲（MP3, FLAC, ジャケット画像, ビジュアライザー動画, 歌詞, 楽譜）を
ブラウザ上で一元管理・連続再生・検索・リネーム・一括ダウンロードできる Web アプリケーションです。
高速並列スキャン（マルチスレッド）とリアルタイム進捗追跡機能を内蔵しています。
"""
import os
import io
import json
import base64
import re
import zipfile
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional, List, Dict, Any

from dotenv import load_dotenv

from fastapi import FastAPI, Request, HTTPException, Query, Response, BackgroundTasks
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

CACHE_FILE = Path("drive_cache.json")

# -----------------------------------------------------------------------------
# 1. Google Drive 管理クライアント
# -----------------------------------------------------------------------------
class DriveManager:
    def __init__(self):
        self.service = None
        self.target_folder_id = ""
        self.token_source = "none"
        self.token_info = None
        self.is_syncing = False
        self.sync_progress = {
            "status": "idle",       # idle, scanning, completed, error
            "current": 0,
            "total": 0,
            "percent": 0,
            "current_song": "",
            "message": "待機中",
            "last_synced": ""
        }
        self.reload_credentials()

    def reload_credentials(self):
        """環境変数 / .env から認証情報を再読み込み"""
        load_dotenv(".env", override=True)
        self.target_folder_id = os.environ.get("TARGET_FOLDER_ID", "").strip()
        user_token_b64 = os.environ.get("USER_TOKEN_B64", "").strip()
        token_json_path = Path("token.json")

        token_info = None
        if user_token_b64:
            try:
                # 余計なクォートや空白を除去
                clean_b64 = user_token_b64.strip('"\'')
                decoded = base64.b64decode(clean_b64).decode("utf-8")
                token_info = json.loads(decoded)
                self.token_source = "env (USER_TOKEN_B64)"
            except Exception as e:
                print(f"[Drive] USER_TOKEN_B64 デコードエラー: {e}")

        if not token_info and token_json_path.exists():
            try:
                token_info = json.loads(token_json_path.read_text(encoding="utf-8"))
                self.token_source = "token.json"
            except Exception as e:
                print(f"[Drive] token.json 読み込みエラー: {e}")

        self.token_info = token_info

        if token_info and self.target_folder_id:
            try:
                from google.oauth2.credentials import Credentials
                from googleapiclient.discovery import build

                creds = Credentials.from_authorized_user_info(
                    token_info, scopes=["https://www.googleapis.com/auth/drive"]
                )
                self.service = build("drive", "v3", credentials=creds)
                print(f"[Drive] 認証成功 ({self.token_source}) - Target Folder: {self.target_folder_id[:8]}...")
            except Exception as e:
                print(f"[Drive] クライアント初期化失敗: {e}")
                self.service = None
        else:
            self.service = None
            if not self.target_folder_id:
                print("[Drive] TARGET_FOLDER_ID が未設定です。")
            if not token_info:
                print("[Drive] 認証情報が見つかりません。デモモードで動作します。")

    @property
    def is_connected(self) -> bool:
        if self.service is None:
            self.reload_credentials()
        return self.service is not None and bool(self.target_folder_id)

    def get_status(self) -> dict:
        return {
            "connected": self.is_connected,
            "target_folder_id": self.target_folder_id or "未設定",
            "token_source": self.token_source,
            "cache_exists": CACHE_FILE.exists(),
            "sync": self.sync_progress,
        }

    def start_background_sync(self):
        """バックグラウンドスレッドで Drive 走査を開始"""
        if self.is_syncing:
            return
        thread = threading.Thread(target=self._run_sync, daemon=True)
        thread.start()

    def _run_sync(self):
        """並列スキャンによる高速同期実行"""
        if not self.is_connected:
            self.sync_progress["status"] = "error"
            self.sync_progress["message"] = "Google Drive に接続されていません"
            return

        self.is_syncing = True
        self.sync_progress = {
            "status": "scanning",
            "current": 0,
            "total": 0,
            "percent": 0,
            "current_song": "フォルダ一覧を取得中...",
            "message": "Google Drive のルートフォルダを走査中...",
            "last_synced": ""
        }

        try:
            # 1. ターゲットフォルダ配下のサブフォルダ一覧を一括取得
            query = f"'{self.target_folder_id}' in parents and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
            res = self.service.files().list(
                q=query,
                spaces="drive",
                fields="files(id, name, createdTime, modifiedTime)",
                pageSize=100,
                orderBy="createdTime desc"
            ).execute()
            raw_folders = res.get("files", [])

            # 集約フォルダ・管理用フォルダを除外
            folders = [
                f for f in raw_folders
                if f["name"].lower() not in ["mp3", "covers", "videos", "archive", "temp"]
            ]

            total = len(folders)
            self.sync_progress["total"] = total
            self.sync_progress["message"] = f"全 {total} 件の楽曲フォルダを並列スキャン中..."

            songs = []
            completed_count = 0

            # 2. ThreadPoolExecutor による並列メタデータ取得 (最大10スレッド)
            def process_folder(folder):
                from google.oauth2.credentials import Credentials
                from googleapiclient.discovery import build
                creds = Credentials.from_authorized_user_info(self.token_info, scopes=["https://www.googleapis.com/auth/drive"])
                thread_service = build("drive", "v3", credentials=creds, cache_discovery=False)
                return self._inspect_folder_assets(folder, thread_service)

            with ThreadPoolExecutor(max_workers=10) as executor:
                future_to_folder = {executor.submit(process_folder, f): f for f in folders}
                for future in as_completed(future_to_folder):
                    folder = future_to_folder[future]
                    try:
                        song_info = future.result()
                        if song_info:
                            songs.append(song_info)
                    except Exception as e:
                        print(f"[Drive] フォルダ解析エラー ({folder['name']}): {e}")

                    completed_count += 1
                    percent = int((completed_count / total) * 100) if total > 0 else 100
                    self.sync_progress["current"] = completed_count
                    self.sync_progress["percent"] = percent
                    self.sync_progress["current_song"] = folder["name"]
                    self.sync_progress["message"] = f"解析中 ({completed_count}/{total} 曲) - {percent}%"

            # 作成日時の新しい順にソート
            songs.sort(key=lambda s: s.get("timestamp", ""), reverse=True)

            # キャッシュ保存 (0件の場合はエラーとみなし、既存キャッシュを保護)
            if len(songs) > 0:
                CACHE_FILE.write_text(json.dumps(songs, ensure_ascii=False, indent=2), encoding="utf-8")
                import datetime
                now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                self.sync_progress["status"] = "completed"
                self.sync_progress["message"] = f"同期完了！ 全 {len(songs)} 曲を取得しました"
                self.sync_progress["last_synced"] = now_str
                print(f"[Drive] 同期成功: 全 {len(songs)} 曲をキャッシュしました")
            else:
                self.sync_progress["status"] = "error"
                self.sync_progress["message"] = "楽曲の解析に失敗しました（0件取得）。キャッシュの保護が適用されました。"
                print("[Drive] 警告: 取得できた楽曲が0件のため、既存キャッシュの上書きを防止しました。")

        except Exception as e:
            print(f"[Drive] 同期全体エラー: {e}")
            self.sync_progress["status"] = "error"
            self.sync_progress["message"] = f"同期エラー: {e}"
        finally:
            self.is_syncing = False

    def _inspect_folder_assets(self, folder: dict, service=None) -> dict:
        """単一フォルダ内のアセットと prompt_info.txt を解析"""
        srv = service or self.service
        folder_id = folder["id"]
        folder_name = folder["name"]

        file_query = f"'{folder_id}' in parents and trashed = false"
        files_res = srv.files().list(
            q=file_query,
            spaces="drive",
            fields="files(id, name, size, mimeType)",
            pageSize=30
        ).execute()
        children = files_res.get("files", [])

        song_info = {
            "id": folder_id,
            "folder_name": folder_name,
            "title": folder_name,
            "timestamp": folder.get("createdTime", ""),
            "style": "AI Generated",
            "theme": "",
            "lyrics_raw": "",
            "lyrics_opt": "",
            "seed": None,
            "trigger": "",
            "mp3_id": None,
            "mp3_size": 0,
            "flac_id": None,
            "flac_size": 0,
            "cover_id": None,
            "video_id": None,
            "abc_id": None,
            "prompt_info_id": None,
        }

        prompt_cid = None
        for child in children:
            cname = child["name"]
            cid = child["id"]
            csize = int(child.get("size", 0))

            if cname.endswith(".mp3"):
                song_info["mp3_id"] = cid
                song_info["mp3_size"] = csize
            elif cname.endswith(".flac"):
                song_info["flac_id"] = cid
                song_info["flac_size"] = csize
            elif cname.endswith((".jpg", ".jpeg", ".png")) and ("cover" in cname or "3000" in cname):
                song_info["cover_id"] = cid
            elif cname.endswith(".mp4"):
                song_info["video_id"] = cid
            elif cname.endswith(".abc"):
                song_info["abc_id"] = cid
            elif cname == "prompt_info.txt":
                song_info["prompt_info_id"] = cid
                prompt_cid = cid

        if prompt_cid:
            try:
                content_bytes = srv.files().get_media(fileId=prompt_cid).execute()
                prompt_text = content_bytes.decode("utf-8", errors="ignore")
                song_info.update(self._parse_prompt_info(prompt_text))
            except Exception as e:
                print(f"[Drive] prompt_info 取得失敗 ({folder_name}): {e}")
        else:
            # フォルダ名から曲名を推測
            parts = folder_name.rsplit("_", 2)
            if len(parts) >= 2:
                song_info["title"] = parts[0]

        return song_info

    def _parse_prompt_info(self, text: str) -> dict:
        """prompt_info.txt からメタデータを抽出"""
        data = {}
        title_m = re.search(r"^Title:\s*(.+)$", text, re.MULTILINE)
        if title_m:
            data["title"] = title_m.group(1).strip()

        style_m = re.search(r"^Style:\s*(.+)$", text, re.MULTILINE)
        if style_m:
            data["style"] = style_m.group(1).strip()

        theme_m = re.search(r"^Theme:\s*(.+)$", text, re.MULTILINE)
        if theme_m:
            data["theme"] = theme_m.group(1).strip()

        seed_m = re.search(r"^Seed:\s*(.+)$", text, re.MULTILINE)
        if seed_m:
            data["seed"] = seed_m.group(1).strip()

        trigger_m = re.search(r"^Trigger:\s*(.+)$", text, re.MULTILINE)
        if trigger_m:
            data["trigger"] = trigger_m.group(1).strip()

        time_m = re.search(r"^Timestamp:\s*(.+)$", text, re.MULTILINE)
        if time_m:
            data["timestamp"] = time_m.group(1).strip()

        orig_match = re.search(r"Original Lyrics:\s*\n(.*?)(?=\n\nOptimized Lyrics|\Z)", text, re.DOTALL)
        if orig_match:
            data["lyrics_raw"] = orig_match.group(1).strip()

        opt_match = re.search(r"Optimized Lyrics.*?:\s*\n(.*)$", text, re.DOTALL)
        if opt_match:
            data["lyrics_opt"] = opt_match.group(1).strip()

        return data

    def rename_song_folder(self, folder_id: str, new_name: str) -> bool:
        """Google Drive 上のフォルダ名を変更"""
        if not self.is_connected:
            return False
        try:
            self.service.files().update(
                fileId=folder_id,
                body={"name": new_name}
            ).execute()
            if CACHE_FILE.exists():
                cached = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
                for s in cached:
                    if s["id"] == folder_id:
                        s["title"] = new_name
                        s["folder_name"] = new_name
                CACHE_FILE.write_text(json.dumps(cached, ensure_ascii=False, indent=2), encoding="utf-8")
            return True
        except Exception as e:
            print(f"[Drive] リネームエラー: {e}")
            return False

    def stream_file_content(self, file_id: str, range_header: Optional[str] = None):
        """Google Drive ファイルをストリーミング中継 (HTTP Range対応)"""
        if not self.is_connected:
            raise HTTPException(status_code=503, detail="Google Drive not connected")

        import requests
        creds = self.service._http.credentials
        if creds.expired and creds.refresh:
            from google.auth.transport.requests import Request as GRequest
            creds.refresh(GRequest())

        url = f"https://www.googleapis.com/drive/v3/files/{file_id}?alt=media"
        headers = {"Authorization": f"Bearer {creds.token}"}
        if range_header:
            headers["Range"] = range_header

        req = requests.get(url, headers=headers, stream=True)
        return req

    def get_cached_songs(self) -> List[Dict[str, Any]]:
        """キャッシュ済みの楽曲一覧を即座に返却"""
        if CACHE_FILE.exists():
            try:
                return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
            except Exception:
                pass
        return self._get_fallback_demo()

    def _get_fallback_demo(self) -> List[Dict[str, Any]]:
        """デモ用サンプル楽曲データ"""
        return [
            {
                "id": "demo_1",
                "folder_name": "midnight_neon_20261015_200000",
                "title": "真夜中のネオンサイン",
                "timestamp": "2026-10-15 20:00:00",
                "style": "Japanese, female vocal, 80s city pop, funky bass, vintage synth, 116 BPM",
                "theme": "雨上がりの首都高を走るノスタルジックなシティポップ",
                "lyrics_raw": "[Intro]\n雨上がりの ハイウェイ\n光るテールランプの列\n\n[Verse 1]\nステレオから 流れるメロディ\nどこか切ない 夜の風\nあの日の言葉 思い出す\n\n[Chorus]\n真夜中の ネオンサイン\n街を染めてく ブルーとピンク\n駆け抜ける ハイウェイに\n面影を探しているの",
                "lyrics_opt": "あめあがりの はい​うぇい\nひかる てーる らんぷの れつ\nすてれおから ながれる めろでぃ\nまよなかの ねおん さいん",
                "seed": "12345",
                "trigger": "定時バッチ",
                "mp3_id": "demo_mp3_1",
                "mp3_size": 4200000,
                "flac_id": "demo_flac_1",
                "flac_size": 28000000,
                "cover_id": "demo_cover_1",
                "video_id": "demo_video_1",
                "abc_id": "demo_abc_1",
                "is_demo": True,
            },
            {
                "id": "demo_2",
                "folder_name": "sakura_drive_20261015_220000",
                "title": "星空のハイウェイ",
                "timestamp": "2026-10-15 22:00:00",
                "style": "Japanese, male vocal, emotive J-Rock, driving electric guitar, punchy drums, 165 BPM",
                "theme": "夜空へ加速していく疾走感あふれるエモーショナルロック",
                "lyrics_raw": "[Intro]\n歪むギターの音 突き抜ける夜風\n\n[Verse 1]\n暗闇の 向こうへ\nアクセルを踏み込むんだ\n迷いなんて 置いていけ\n\n[Chorus]\n星空の ハイウェイを\n誰よりも速く駆け抜けろ\n胸の中 燃える火は\n消せやしないから",
                "lyrics_opt": "ゆがむ ぎたーの おと\nつきぬける よかぜ\nくらやみの むこうへ\nほしぞらの はい​うぇい",
                "seed": "67890",
                "trigger": "Web UI",
                "mp3_id": "demo_mp3_2",
                "mp3_size": 3900000,
                "flac_id": "demo_flac_2",
                "flac_size": 26000000,
                "cover_id": "demo_cover_2",
                "video_id": "demo_video_2",
                "abc_id": "demo_abc_2",
                "is_demo": True,
            }
        ]

drive_mgr = DriveManager()

# -----------------------------------------------------------------------------
# 2. FastAPI アプリケーション定義
# -----------------------------------------------------------------------------
app = FastAPI(title="YuE2 Studio Manager", version="1.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# -----------------------------------------------------------------------------
# 3. API エンドポイント
# -----------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
async def get_index():
    """管理ダッシュボード HTML を返却"""
    html_path = Path("templates/index.html")
    if html_path.exists():
        return HTMLResponse(content=html_path.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>YuE2 Manager: templates/index.html が見つかりません</h1>")

@app.get("/api/stats")
async def get_stats():
    """楽曲ライブラリのジャンル、ボーカル、テンポ、雰囲気の統計情報を返却"""
    songs = drive_mgr.get_cached_songs()
    from collections import Counter

    total_songs = len(songs)
    with_video = sum(1 for s in songs if s.get("video_id"))
    with_flac = sum(1 for s in songs if s.get("flac_id"))
    total_bytes = sum(int(s.get("mp3_size", 0)) + int(s.get("flac_size", 0)) for s in songs)

    # 1. ジャンル集計
    genre_keywords = [
        "Pop", "Acoustic", "Rock", "R&B", "Jazz", "Folk", "House", "Eurobeat",
        "Funk", "Lo-Fi", "Tropical House", "Synthwave", "Retrowave", "City Pop",
        "Neo Soul", "Future Bass", "Reggae", "Bossa Nova", "EDM", "Ballad", "Hip Hop"
    ]
    genre_counts = Counter()
    for s in songs:
        style = s.get("style", "").lower()
        matched = False
        for g in genre_keywords:
            if re.search(r"\b" + re.escape(g.lower()) + r"\b", style):
                genre_counts[g] += 1
                matched = True
        if not matched:
            genre_counts["Other"] += 1

    # 2. ボーカル集計
    vocal_counts = Counter()
    for s in songs:
        st = s.get("style", "").lower()
        if "female vocal" in st:
            vocal_counts["女性ボーカル (Female)"] += 1
        elif "male vocal" in st:
            vocal_counts["男性ボーカル (Male)"] += 1
        elif "duet" in st:
            vocal_counts["デュエット (Duet)"] += 1
        else:
            vocal_counts["インスト / その他"] += 1

    # 3. BPM（テンポ）集計
    bpms = []
    bpm_ranges = {"slow": 0, "mid": 0, "fast": 0}
    for s in songs:
        m = re.search(r"(\d+)\s*BPM", s.get("style", ""), re.IGNORECASE)
        if m:
            val = int(m.group(1))
            bpms.append(val)
            if val < 100:
                bpm_ranges["slow"] += 1
            elif val <= 130:
                bpm_ranges["mid"] += 1
            else:
                bpm_ranges["fast"] += 1

    avg_bpm = round(sum(bpms) / len(bpms), 1) if bpms else 0

    # 4. 雰囲気・ムード集計（themeおよびlyricsから抽出）
    mood_keywords = [
        ("夜", "🌙 夜・ミッドナイト"),
        ("疾走", "⚡ 疾走感・ドライブ"),
        ("光", "✨ 光・希望"),
        ("夏", "☀️ 夏・サマー"),
        ("雨", "🌧️ 雨・切ない"),
        ("朝", "🌅 朝・爽やか"),
        ("風", "🍃 風・開放感"),
        ("旅", "🚗 旅・トリップ"),
        ("海", "🌊 海・ビーチ"),
        ("空", "☁️ 空・スカイ"),
        ("未来", "🚀 未来・サイバー"),
        ("エモーショナル", "🎆 エモーショナル"),
        ("カフェ", "☕ カフェ・チル"),
        ("ノスタルジック", "📻 ノスタルジック")
    ]
    mood_counts = []
    for raw_word, label in mood_keywords:
        cnt = sum(1 for s in songs if raw_word in s.get("theme", "") or raw_word in s.get("title", ""))
        if cnt > 0:
            mood_counts.append({"raw": raw_word, "label": label, "count": cnt})
    mood_counts.sort(key=lambda x: x["count"], reverse=True)

    return {
        "summary": {
            "total_songs": total_songs,
            "with_video": with_video,
            "with_flac": with_flac,
            "total_size_mb": round(total_bytes / (1024 * 1024), 1),
            "avg_bpm": avg_bpm,
        },
        "genres": [{"name": k, "count": v} for k, v in genre_counts.most_common() if v > 0],
        "vocals": [{"name": k, "count": v} for k, v in vocal_counts.items()],
        "bpm_ranges": {
            "slow": {"label": "スロー (<100)", "count": bpm_ranges["slow"]},
            "mid": {"label": "ミディアム (100-130)", "count": bpm_ranges["mid"]},
            "fast": {"label": "アップテンポ (>130)", "count": bpm_ranges["fast"]},
        },
        "moods": mood_counts
    }

@app.get("/api/status")
async def get_status():
    """Drive 接続状態と環境設定・同期進捗を返却"""
    drive_mgr.reload_credentials()
    return drive_mgr.get_status()

@app.get("/api/sync/progress")
async def get_sync_progress():
    """現在の同期進捗ステータスを返却"""
    return drive_mgr.sync_progress

@app.post("/api/sync/start")
async def start_sync():
    """バックグラウンド同期を開始"""
    drive_mgr.reload_credentials()
    if not drive_mgr.is_connected:
        raise HTTPException(status_code=400, detail="Google Drive が接続されていません。.env を確認してください。")
    if drive_mgr.is_syncing:
        return {"status": "already_syncing", "progress": drive_mgr.sync_progress}
    drive_mgr.start_background_sync()
    return {"status": "started", "progress": drive_mgr.sync_progress}

@app.get("/api/songs")
async def get_songs(refresh: bool = False):
    """楽曲一覧を取得（キャッシュまたは即時バックグラウンド同期）"""
    if refresh:
        drive_mgr.reload_credentials()
        if drive_mgr.is_connected and not drive_mgr.is_syncing:
            drive_mgr.start_background_sync()

    songs = drive_mgr.get_cached_songs()
    return {
        "songs": songs,
        "total": len(songs),
        "is_connected": drive_mgr.is_connected,
        "sync": drive_mgr.sync_progress,
    }

@app.get("/api/stream/{file_id}")
async def stream_audio(file_id: str, request: Request):
    """MP3 / 音声のストリーミング中継 (HTTP Range 対応)"""
    if file_id.startswith("demo_"):
        raise HTTPException(status_code=404, detail="Demo audio")

    range_header = request.headers.get("Range")
    try:
        drive_resp = drive_mgr.stream_file_content(file_id, range_header=range_header)
        status_code = drive_resp.status_code
        headers = {}
        for k in ["Content-Type", "Content-Length", "Content-Range", "Accept-Ranges"]:
            if k in drive_resp.headers:
                headers[k] = drive_resp.headers[k]
        headers["Accept-Ranges"] = "bytes"

        return StreamingResponse(
            drive_resp.iter_content(chunk_size=64 * 1024),
            status_code=status_code,
            headers=headers
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/media/{file_id}")
async def get_media(file_id: str, request: Request):
    """ジャケット画像や動画のプロキシストリーミング"""
    if file_id.startswith("demo_"):
        raise HTTPException(status_code=404, detail="Demo media")

    range_header = request.headers.get("Range")
    try:
        drive_resp = drive_mgr.stream_file_content(file_id, range_header=range_header)
        status_code = drive_resp.status_code
        headers = {}
        for k in ["Content-Type", "Content-Length", "Content-Range", "Accept-Ranges"]:
            if k in drive_resp.headers:
                headers[k] = drive_resp.headers[k]

        return StreamingResponse(
            drive_resp.iter_content(chunk_size=128 * 1024),
            status_code=status_code,
            headers=headers
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

class RenameRequest(BaseModel):
    new_name: str

@app.post("/api/songs/{folder_id}/rename")
async def rename_song(folder_id: str, req: RenameRequest):
    """Google Drive 上のフォルダ名を変更"""
    success = drive_mgr.rename_song_folder(folder_id, req.new_name)
    if not success:
        if CACHE_FILE.exists():
            cached = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
            for s in cached:
                if s["id"] == folder_id:
                    s["title"] = req.new_name
                    s["folder_name"] = req.new_name
            CACHE_FILE.write_text(json.dumps(cached, ensure_ascii=False, indent=2), encoding="utf-8")
            return {"status": "ok", "mode": "cache_only"}
        raise HTTPException(status_code=400, detail="Rename failed")
    return {"status": "ok"}

class DownloadRequest(BaseModel):
    song_ids: List[str]
    package_type: str = "mp3"  # mp3, full, video_mp3

@app.post("/api/download/zip")
async def download_zip(req: DownloadRequest):
    """選択した複数楽曲の ZIP 一括ダウンロード"""
    if not CACHE_FILE.exists():
        raise HTTPException(status_code=404, detail="Cache not found")

    cached = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    id_map = {s["id"]: s for s in cached}

    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zip_file:
        for sid in req.song_ids:
            song = id_map.get(sid)
            if not song:
                continue

            folder_name = song.get("folder_name", sid)
            title = song.get("title", folder_name)

            # MP3
            if song.get("mp3_id") and not song["mp3_id"].startswith("demo_"):
                try:
                    data = drive_mgr.service.files().get_media(fileId=song["mp3_id"]).execute()
                    zip_file.writestr(f"{title}/{title}.mp3", data)
                except Exception as e:
                    print(f"Error fetching MP3 for {title}: {e}")

            # 配信フルパック (FLAC, ジャケット, 歌詞)
            if req.package_type == "full":
                if song.get("flac_id") and not song["flac_id"].startswith("demo_"):
                    try:
                        data = drive_mgr.service.files().get_media(fileId=song["flac_id"]).execute()
                        zip_file.writestr(f"{title}/{title}.flac", data)
                    except Exception:
                        pass
                if song.get("cover_id") and not song["cover_id"].startswith("demo_"):
                    try:
                        data = drive_mgr.service.files().get_media(fileId=song["cover_id"]).execute()
                        zip_file.writestr(f"{title}/cover_3000.jpg", data)
                    except Exception:
                        pass
                lyrics_text = f"Title: {title}\nStyle: {song.get('style')}\n\n[Original Lyrics]\n{song.get('lyrics_raw')}\n\n[Optimized Lyrics]\n{song.get('lyrics_opt')}"
                zip_file.writestr(f"{title}/lyrics.txt", lyrics_text.encode("utf-8"))

            # 動画＋MP3
            if req.package_type == "video_mp3":
                if song.get("video_id") and not song["video_id"].startswith("demo_"):
                    try:
                        data = drive_mgr.service.files().get_media(fileId=song["video_id"]).execute()
                        zip_file.writestr(f"{title}/{title}_visualizer.mp4", data)
                    except Exception:
                        pass

    zip_buffer.seek(0)
    filename = f"yue2_export_{len(req.song_ids)}_songs.zip"
    return StreamingResponse(
        zip_buffer,
        media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )

# -----------------------------------------------------------------------------
# 4. ローカル実行エントリーポイント
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    print(f"\n=======================================================")
    print(f"🎵 YuE2 Studio Manager (v1.1 高速並列スキャン版) を起動中...")
    print(f"🌐 ブラウザでアクセス: http://localhost:{port}")
    print(f"=======================================================\n")
    uvicorn.run("manager:app", host="0.0.0.0", port=port, reload=True)
