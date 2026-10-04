"""
Discord Webhook 疎通テストスクリプト (test_discord.py)
Modal Secret (discord-secret) の設定が正しく動作しているかをCPU単体で検証します。
"""
import modal
import os
import datetime

app = modal.App("test-discord-notification")
image = modal.Image.debian_slim().pip_install("requests")

@app.function(image=image, secrets=[modal.Secret.from_name("discord-secret")])
def test_discord_notify():
    import requests

    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if not webhook_url:
        return "⚠️ DISCORD_WEBHOOK_URL が未設定（または空文字）です。\n設定コマンド: modal secret create discord-secret DISCORD_WEBHOOK_URL=\"https://discord.com/api/webhooks/...\" --force"

    embed = {
        "title": "🎵 [テスト] Discord 通知連携の疎通確認",
        "description": "YuE2 × Modal 楽曲自動生成パイプラインからの通知テストです。\nこのメッセージが表示されていれば、連携設定は完了しています。",
        "color": 0x1A73E8,
        "fields": [
            {
                "name": "⚙️ 稼働状態",
                "value": "Modal クラウド環境からの送信: **正常**",
                "inline": False,
            },
            {
                "name": "📁 Google Drive 連携",
                "value": "生成完了後の自動アップロード＆リンク通知に対応",
                "inline": False,
            },
        ],
        "footer": {
            "text": "YuE2 × Modal 楽曲自動生成パイプライン (Phase 4)",
        },
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }

    payload = {
        "content": "🔔 **YuE2 通知テスト**",
        "embeds": [embed],
    }

    try:
        res = requests.post(webhook_url, json=payload, timeout=10)
        if res.status_code in (200, 204):
            return "🎉 大成功！ Discord への通知送信に成功しました！"
        else:
            return f"❌ エラー: HTTP {res.status_code} - {res.text}"
    except Exception as e:
        return f"❌ 送信例外エラー: {e}"

@app.local_entrypoint()
def main():
    print("Discord Webhook 疎通テストをクラウド上で実行中...")
    result = test_discord_notify.remote()
    print(result)
