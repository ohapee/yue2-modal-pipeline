"""
Google Drive mp3 フォルダ連携および同期テストスクリプト (test_mp3_sync.py)
CPU単体・数秒で動作し、Google Drive上に 'mp3' フォルダが存在・作成可能かを検証します。
"""
import modal
import os
import json
import base64
import io

app = modal.App("test-mp3-sync")
image = modal.Image.debian_slim().apt_install("ffmpeg").pip_install("google-api-python-client", "google-auth")

@app.function(image=image, secrets=[modal.Secret.from_name("google-drive-secret")])
def test_mp3_folder_check():
    user_token_b64 = os.environ.get("USER_TOKEN_B64", "")
    target_id = os.environ.get("TARGET_FOLDER_ID", "")

    if not user_token_b64 or not target_id:
        return "ERROR: USER_TOKEN_B64 または TARGET_FOLDER_ID が未設定です。"

    try:
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build
        from googleapiclient.http import MediaIoBaseUpload

        token_info = json.loads(base64.b64decode(user_token_b64).decode("utf-8"))
        creds = Credentials.from_authorized_user_info(token_info, scopes=["https://www.googleapis.com/auth/drive"])
        service = build("drive", "v3", credentials=creds)

        # 1. 'mp3' フォルダの検索または作成
        query = f"'{target_id}' in parents and name = 'mp3' and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
        res = service.files().list(q=query, spaces="drive", fields="files(id, name)").execute()
        files = res.get("files", [])

        if files:
            mp3_id = files[0]["id"]
            status_msg = f"既存の 'mp3' フォルダを検出しました (ID: {mp3_id})"
        else:
            meta = {
                "name": "mp3",
                "mimeType": "application/vnd.google-apps.folder",
                "parents": [target_id]
            }
            folder = service.files().create(body=meta, fields="id").execute()
            mp3_id = folder.get("id")
            status_msg = f"新規に 'mp3' フォルダを作成しました (ID: {mp3_id})"

        # 2. mp3 フォルダ内のファイル一覧を取得
        list_res = service.files().list(q=f"'{mp3_id}' in parents and trashed = false", fields="files(name)").execute()
        mp3_files = [f.get("name") for f in list_res.get("files", [])]

        return (
            f"🎉 大成功！ Google Drive 上の mp3 フォルダ連携は正常です！\n"
            f"【状態】 {status_msg}\n"
            f"【現在のMP3ファイル数】 {len(mp3_files)} 件\n"
            f"【ファイル一覧】 {mp3_files[:5]}"
        )
    except Exception as e:
        return f"ERROR: {e}"

@app.local_entrypoint()
def main():
    print("Google Drive 'mp3' フォルダの診断を実行中...")
    result = test_mp3_folder_check.remote()
    print(result)
