"""
Modal Secret から Google Drive 認証情報を取得し、ローカルの .env に保存するスクリプト
使い方: modal run export_secrets.py
"""
import os
from pathlib import Path
import modal

app = modal.App("export-yue2-secrets")

@app.function(secrets=[modal.Secret.from_name("google-drive-secret")])
def fetch_drive_secrets():
    """Modal クラウド上で Secret を取得"""
    user_token = os.environ.get("USER_TOKEN_B64", "")
    target_id = os.environ.get("TARGET_FOLDER_ID", "")
    return {
        "USER_TOKEN_B64": user_token,
        "TARGET_FOLDER_ID": target_id,
    }

@app.local_entrypoint()
def main():
    print("クラウド上の Modal Secret (google-drive-secret) から認証情報を取得中...")
    secrets = fetch_drive_secrets.remote()

    target_id = secrets.get("TARGET_FOLDER_ID", "")
    user_token = secrets.get("USER_TOKEN_B64", "")

    if not target_id or not user_token:
        print("❌ エラー: Secret 内に TARGET_FOLDER_ID または USER_TOKEN_B64 が見つかりませんでした。")
        return

    env_content = (
        f"# YuE2 Google Drive Manager 自動生成設定ファイル\n"
        f'TARGET_FOLDER_ID="{target_id}"\n'
        f'USER_TOKEN_B64="{user_token}"\n'
    )

    env_path = Path(".env")
    env_path.write_text(env_content, encoding="utf-8")
    print(f"🎉 成功！ローカルの .env に認証情報を保存しました: {env_path.resolve()}")
    print(f"・TARGET_FOLDER_ID: {target_id}")
    print(f"・USER_TOKEN_B64: (設定完了 / 長さ: {len(user_token)} 文字)")
    print("\n管理ツール（http://localhost:8000）を開き、右上の「🔄 再同期」を押すと Google Drive の実データが読み込まれます！")
