# YuE2 × Modal クラウド楽曲自動生成パイプライン

オープンソース最先端のAI音楽生成モデル「**YuE2 (3B)**」を、サーバーレスGPU基盤「**Modal**」上で稼働させ、**定期自動生成（Cron）**、**Google Drive自動保存（FLAC & 192kbps MP3）**、**Geminiによる多ジャンル自律作詞（最大2分58秒本格構成）**、**Discord完了通知**、そして **Web/スマホからのオンデマンド生成** を完全クラウド完結（ローカルGPU・常時電源不要）で実現するパイプラインシステムです。

---

## 📌 プロジェクト概要

従来のAI楽曲生成（Suno/Udio等）はクローズドなWebサービスが主流でしたが、本プロジェクトではオープンモデル「YuE2」の記号的計画（ABC記譜法）能力と、秒単位課金のサーバーレスクラウドGPU（NVIDIA L4 24GB）を組み合わせることで、**常時起動コストゼロ・ローカルPCリソース不要** の持続可能な音楽制作環境を実現しています。

さらに、日本語歌詞特有の「音節ズレによるハルシネーション（歌詞の勝手な置き換わり）」を防ぐため、**形態素解析による「ひらがな化・分かち書き・5〜8文字改行」の自動最適化エンジン** を内蔵しています。

---

## 🌟 主な特徴

1. **サーバーレスGPU推論（常時コストゼロ）**
   - NVIDIA L4（24GB VRAM）を推論時のみ動的確保（1曲あたり約2〜3分、約5円〜8円）。
   - 待機費用は一切発生せず、Modalの月額$30無料クレジット枠内で完全運用可能。
2. **多ジャンル自律作詞 ＆ 本格7セクション構成（Gemini API）**
   - **28種以上の多彩なジャンル**（J-Pop、City Pop、J-Rock、Lo-Fi、Neo Soul、Future Bass、ボサノバ、和風ポップ、レゲエ、ユーロビート等）× **10種のボーカル表現** を自律サンプリング。
   - `[Intro]`, `[Verse 1]`, `[Pre-Chorus]`, `[Chorus]`, `[Verse 2]`, `[Chorus]`, `[Outro]` の王道構成により、聴き応えのある豊かな展開を実現。
3. **最大2分58秒（178秒）厳格時間制御（FFmpeg フェードアウト保証）**
   - 曲の長さが「**最大2分58秒（178秒以内）**」に収まるよう歌詞ボリュームを最適設計。
   - 万が一178秒を超えた場合でも、終了前5秒間（173〜178秒）で自然にフェードアウトさせて確実に2分58秒以内で美しく終了。
4. **日本語歌詞の自動最適化エンジン (`pykakasi`)**
   - 漢字混じり・長文の歌詞を、GPU投入直前に「ひらがな・分かち書き・1行5〜8文字」へ自動整形。
   - ABC楽譜（音符）とモーラ（拍数）の1対1対応を強制し、音節崩れによるハルシネーションを防止。
5. **高音質 192kbps MP3 自動変換 & Google Drive 3大集約フォルダ保存 (`mp3/`, `covers/`, `videos/`)**
   - 生成された FLAC 音源から 192kbps MP3 を自動変換。
   - 各曲の個別フォルダ（`yue2/曲名_日時/`）への保存に加え、共有フォルダ **`yue2/mp3/`（全楽曲MP3）**、**`yue2/covers/`（全3000pxジャケット画像）**、**`yue2/videos/`（全フルHD動画）** に自動集約。
   - Google Drive 上で全曲の音楽再生、ジャケット一覧ギャラリー、動画のワンタップ試聴・SNS共有が極めて快適に行えます。
6. **配信規格 3000×3000px AIジャケット画像自動生成**
   - Gemini 画像生成モデル（`gemini-2.5-flash-image`）と FFmpeg（Lanczos補間）の連携により、楽曲の世界観・歌詞に合致した正方形 3000×3000px 配信審査準拠アートワークを完全自動生成。
7. **フルHD 1080p 波形ビジュアライザー動画自動生成 (FFmpeg MP4)**
   - 背景ブラー、前面正方形ジャケット、音楽のビートに合わせてリアルタイムに躍動するネオンシアン波形（`showwaves`）を合成した 1920×1080 16:9 MP4 動画を自動レンダリング。
8. **Discord Webhook リッチ完了通知 (マルチメディア対応)**
   - 楽曲生成、ジャケット画像、動画生成が完了すると、Discordチャンネルへ試聴リンク・成果物セット・歌詞カード付きのEmbedカードを即座に送信。
9. **Google Drive 自動バックアップ（OAuth 2.0 連携）**
   - 個人アカウントの空き容量を直接利用し、生成完了後にFLAC、MP3、ジャケットJPG、動画MP4、ABC楽譜、生成ログを専用フォルダ（`yue2`）へ自動転送。
10. **Modal ストレージ自動クリーンアップ（5GB超過時自動パージ / 毎回削除対応）**
   - Google Drive へのアップロードが確実に成功したことをトリガーに、Modal Volume の容量を自動監視。
   - **5GB上限モード（デフォルト）**: 5GBを超過した場合、Google Drive転送済みの古い楽曲から安全に自動削除（直近の曲はマスターとして保護）。
   - **毎回削除モード（`MODAL_STORAGE_MODE=purge_immediately`）**: 転送完了後、Modal Volume側からは即座に削除しディスク使用量0MBを維持。
11. **完全クラウド完結のトリプル・トリガー ＆ バッチ同期**
   - **Web UI (ASGI / FastAPI)**: スマホ・ブラウザ対応のレスポンシブ画面。「AIにおまかせ生成」と「自由作詞生成」をタブで切り替え可能。
   - **定時バッチ生成 (Modal Cron)**: 2時間毎（偶数時の0分）に完全自動でAI作詞から音源・ジャケット・動画生成・Drive保存・通知まで一括実行。
   - **日次MP3一括同期バッチ (Modal Cron)**: 毎日深夜（JST 24:00）に未変換のFLACを自動スキャンし、MP3化してDriveへ同期。
   - **ローカルCLI**: パラメータ検証や手元テスト用（`modal run app.py`）。

---

## 🗺️ ロードマップと進捗状況

詳細な設計書および運用仕様は、Google Drive上のドキュメントにて管理・更新しています。  
📄 [**YuE2_Modal_楽曲自動生成パイプライン_ロードマップ・運用設計書（Google Drive）**](https://docs.google.com/document/d/1xXgRRyAVwIXKB-dIimDwWwdMl55rxDMn9tvui48THPY/edit)

| フェーズ | マイルストーン | 主な機能・対応内容 | 状態 |
| :--- | :--- | :--- | :---: |
| **Phase 1** | **基盤構築 & プロトタイプ検証** | Modalアカウント連携、NVIDIA L4動作確認、YuE2-3BによるFLAC/ABC生成成功、モデルVolumeキャッシュ | ✅ **完了** |
| **Phase 2** | **クラウド完結化 & 歌詞最適化** | FastAPI Web UI、Cron週次定期実行、日本語歌詞最適化エンジン（pykakasi） | ✅ **完了** |
| **Phase 3** | **Google Drive 自動連携** | OAuth 2.0 個人権限連携、yue2フォルダへの自動同期（音源・楽譜・ログ） | ✅ **完了** |
| **Phase 4** | **定期実行の高度化 & 多ジャンル展開・容量管理** | Gemini自律作詞（28種以上ジャンル・王道7セクション構成）、最大2分58秒制限、2時間毎Cron生成、Discord通知、192kbps MP3変換 & Drive集約保存、日次一括同期、Modalストレージ自動クリーンアップ（5GB超過パージ／毎回削除） | ✅ **完了** |
| **Phase 5** | **マルチメディア展開 & 収益化** | 3000×3000pxジャケット画像自動生成、1080pビジュアライザー動画自動生成、Google Drive 3大集約（mp3/covers/videos）、配信パッケージ準備 | 🚀 **試験運用中<br>(パターンA: 1週間運用)** |

---

## 📁 ディレクトリ構成

```text
yue2_modal/
├── app.py              # パイプライン本体（L4推論, Web UI, Cron, 歌詞最適化, 時間制限, MP3変換, Drive同期, Discord通知）
├── test_drive.py       # Google Drive 接続診断スクリプト（CPU・数秒で動作）
├── test_mp3_sync.py    # Google Drive mp3 フォルダ疎通確認スクリプト（CPU・数秒で動作）
├── test_modal_gemini.py# Gemini API 自律作詞のクラウド動作確認スクリプト
├── test_discord.py     # Discord Webhook 通知の疎通確認スクリプト
├── test_gemini.py      # ローカル用 Gemini 作成検証スクリプト（新プロンプト・多ジャンル対応）
├── test_gpu.py         # GPU単体動作検証スクリプト
├── ROADMAP.md          # プロジェクト詳細ロードマップ
├── README.md           # 本仕様書・ドキュメント
├── .gitignore          # 秘密情報・キャッシュ除外設定
└── outputs/            # ローカルCLI実行時の保存先（自動生成）
```

---

## ⚙️ Modal Secret（環境変数）の設定

本パイプラインでは、以下の3つの Modal Secret を使用します。

| Secret 名 | 含まれる環境変数 | 用途 | 設定コマンド例 |
| :--- | :--- | :--- | :--- |
| `google-drive-secret` | `USER_TOKEN_B64`<br>`TARGET_FOLDER_ID` | Google Drive 自動アップロード認証 | 登録済み（`test_drive.py` で確認可） |
| `gemini-secret` | `GEMINI_API_KEY` | Gemini API による自律作詞・スタイル生成 | `modal secret create gemini-secret GEMINI_API_KEY="AIzaSy..." --force` |
| `discord-secret` | `DISCORD_WEBHOOK_URL` | 楽曲生成完了時の Discord 通知 | `modal secret create discord-secret DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/..." --force` |

---

## 🚀 セットアップと使い方

### 1. 仮想環境の準備
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install modal google-genai requests pykakasi fastapi python-multipart
modal setup
```

### 2. 各種接続・機能診断（CPU・数秒で実行可能）
```bash
# Google Drive 接続テスト
modal run test_drive.py

# Google Drive mp3 フォルダ連携テスト
modal run test_mp3_sync.py

# Gemini 自律作詞テスト（多ジャンル＆2分58秒設計）
python test_gemini.py

# Discord 通知テスト
modal run test_discord.py
```

### 3. クラウド常駐デプロイ（Web UI & Cron有効化）
```bash
modal deploy app.py
```
デプロイが完了すると、専用のWeb URLが表示されます：
```text
✓ Deployed app yue2-song-generator
├── Function: scheduled_batch_generation (Cron: 0 */2 * * *)
├── Function: sync_flac_to_mp3_batch (Cron: 0 15 * * *)
└── Web function: web -> https://<workspace>--yue2-song-generator-web.modal.run
```
URLをスマートフォンやPCブラウザで開き、**「🤖 AIおまかせ生成」** または **「✍️ 自由作詞」** からワンタップで生成を開始できます。

### 4. 過去楽曲の MP3 一括同期を手動実行
```bash
modal run app.py::sync_flac_to_mp3_batch
```
※毎日深夜（JST 24:00）に定期自動実行されますが、手動で即時一括同期することも可能です。

### 5. ローカルCLIからの直接テスト実行
```bash
# AIにおまかせで自律作詞して生成
modal run app.py --auto-ai --theme "星空のハイウェイ"

# 歌詞・スタイルを手動指定して生成
modal run app.py --title "my_song" --style "Japanese, female vocal, 80s city pop, 116 BPM"
```

### 6. クラウド保存曲の手元ダウンロード
```bash
# 生成曲一覧の確認
modal volume ls yue2-generated-songs

# ローカルフォルダへ一括ダウンロード
modal volume get yue2-generated-songs / outputs_cloud/
```

---

## 🔒 コストと安全設計

- **厳格なタイムアウト管理**: GPU関数に `timeout=600`（10分）を設定し、プロセススタックによる過剰課金を完全に防止。
- **2分58秒厳格トリミング**: 全ての音声（FLAC / MP3）が確実に178秒以内で自然にフェードアウト終了。
- **GPU時間の最小化**: LLMによる作詞（Gemini）、時間トリミング・MP3変換（FFmpeg）、日次バッチ同期、Discord通知はすべて安価なCPUコンテナ上で完結させてからGPUを起動するため、高価なL4 GPU課金時間を1秒も無駄にしません。
- **秘密情報の分離保護**: Google認証トークン、Gemini APIキー、Discord Webhookはすべて `modal.Secret` で暗号化管理され、コード内やGitには一切含まれません。