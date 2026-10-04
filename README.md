# YuE2 × Modal クラウド楽曲自動生成パイプライン

オープンソースの最先端AI音楽生成モデル「**YuE2 (3B)**」を、サーバーレスGPU基盤「**Modal**」上で稼働させ、**定期自動生成（Cron）** と **Web/スマホからのオンデマンド生成** を完全クラウド完結させるパイプラインプロジェクトです。

---

## 📌 プロジェクト概要

従来のAI楽曲生成（Suno/Udio等）はクローズドなWebサービスが主流でしたが、本プロジェクトではオープンモデル「YuE2」の記号的計画（ABC記譜法）能力と、秒単位課金のサーバーレスクラウドGPU（NVIDIA L4 24GB）を組み合わせることで、**常時起動コストゼロ・ローカルGPUリソース不要** の持続可能な音楽制作環境を実現しています。

さらに、日本語歌詞特有の「音節ズレによるハルシネーション（歌詞の勝手な置き換わり）」を防ぐため、**形態素解析による「ひらがな化・分かち書き・5〜8文字改行」の自動プリプロセス機能** を組み込んでいます。

---

## 🌟 主な特徴

1. **サーバーレスGPU推論（常時コストゼロ）**  
   - NVIDIA L4（24GB VRAM）を推論時のみ動的確保（1曲あたり約2分、約4円〜8円）。  
   - 待機費用は一切発生せず、Modalの月額\$30無料クレジット枠内で運用可能。  
2. **完全クラウド完結のトリプル・インターフェース**  
   - **Web UI (ASGI / FastAPI)**: スマートフォンやPCのブラウザから専用URLを開き、ワンタップで生成。  
   - **定時バッチ生成 (Modal Cron)**: 週次などで完全自動のストック楽曲生成。  
   - **ローカルCLI**: 開発・検証用の手元テスト（`modal run app.py`）。  
3. **日本語歌詞の自動最適化エンジン (`pykakasi`)**  
   - 通常の漢字混じり・長文の歌詞を入力しても、GPUに渡る直前に「ひらがな・分かち書き・1行5〜8文字」へ自動整形。  
   - メロディ音符（ABC楽譜）とモーラ（拍数）の1対1対応を強制し、歌詞の再現性を最大化。  
4. **クラウドストレージ永続化 (Modal Volume)**  
   - モデル重み（約4GB）は `yue2-model-cache` にキャッシュしてコールドスタートを短縮。  
   - 生成された音声（FLAC）、楽譜（`score.abc`）、プロンプト設定ログ（`prompt_info.txt`）は `yue2-generated-songs` に自動保管。

---

## 🗺️ ロードマップと進捗状況

詳細な設計書および運用仕様は、Google Drive上のドキュメントにて管理・更新しています。  
📄 [**YuE2\_Modal\_楽曲自動生成パイプライン\_ロードマップ・運用設計書（Google Drive）**](https://docs.google.com/document/d/1xXgRRyAVwIXKB-dIimDwWwdMl55rxDMn9tvui48THPY/edit)

| フェーズ | マイルストーン | 状態 |
| :---- | :---- | :---: |
| **Phase 1** | Modal基盤構築 & CLIプロトタイプによる1曲生成検証 | ✅ 完了 |
| **Phase 2** | クラウド完結化（Cron定期実行 ＋ Web UI手動トリガー ＋ 歌詞自動最適化） | ✅ 完了 |
| **Phase 3** | Google Drive API 連携による生成音源の自動アップロード | ⏳ 次期 |
| **Phase 4** | 歌詞・スタイルの自動ローテーション生成 ＆ Discord通知連携 | 📋 計画 |
| **Phase 5** | ジャケット画像（3000×3000px）・リリック動画生成連携（YouTube / DistroKid） | 📋 計画 |

---

## 📁 ディレクトリ構成

yue2\_modal/

├── app.py              \# クラウド完結型 Modal アプリケーション（Web UI, Cron, GPU推論）

├── README.md           \# 本ドキュメント（下記本文を参照）

- **YuE2 × Modal クラウド楽曲自動生成パイプライン**  
  - オープンソースの最先端AI音楽生成モデル「**YuE2 (3B)**」を、サーバーレスGPU基盤「**Modal**」上で稼働させ、**定期自動生成（Cron）** と **Web/スマホからのオンデマンド生成** を完全クラウド完結させるパイプラインプロジェクトです。  
  - **📌 プロジェクト概要**  
    - 従来のAI楽曲生成（Suno/Udio等）はクローズドなWebサービスが主流でしたが、本プロジェクトではオープンモデル「YuE2」の記号的計画（ABC記譜法）能力と、秒単位課金のサーバーレスクラウドGPU（NVIDIA L4 24GB）を組み合わせることで、**常時起動コストゼロ・ローカルGPUリソース不要** の持続可能な音楽制作環境を実現しています。  
    - さらに、日本語歌詞特有の「音節ズレによるハルシネーション（歌詞の勝手な置き換わり）」を防ぐため、**形態素解析による「ひらがな化・分かち書き・5〜8文字改行」の自動プリプロセス機能** を組み込んでいます。  
  - **🌟 主な特徴**  
    - **1\. サーバーレスGPU推論（常時コストゼロ）**  
      - NVIDIA L4（24GB VRAM）を推論時のみ動的確保（1曲あたり約2分、約4円〜8円）。  
      - 待機費用は一切発生せず、Modalの月額\$30無料クレジット枠内で運用可能。  
    - **2\. 完全クラウド完結のトリプル・インターフェース**  
      - **Web UI (ASGI / FastAPI)**: スマートフォンやPCのブラウザから専用URLを開き、ワンタップで生成。  
      - **定時バッチ生成 (Modal Cron)**: 週次などで完全自動のストック楽曲生成。  
      - **ローカルCLI**: 開発・検証用の手元テスト（`modal run app.py`）。  
    - **3\. 日本語歌詞の自動最適化エンジン (`pykakasi`)**  
      - 通常の漢字混じり・長文の歌詞を入力しても、GPUに渡る直前に「ひらがな・分かち書き・1行5〜8文字」へ自動整形。  
      - メロディ音符（ABC楽譜）とモーラ（拍数）の1対1対応を強制し、歌詞の再現性を最大化。  
    - **4\. クラウドストレージ永続化 (Modal Volume)**  
      - モデル重み（約4GB）は `yue2-model-cache` にキャッシュしてコールドスタートを短縮。  
      - 生成された音声（FLAC）、楽譜（`score.abc`）、プロンプト設定ログ（`prompt_info.txt`）は `yue2-generated-songs` に自動保管。  
  - **🗺️ ロードマップと進捗状況**  
    - 詳細な設計書および運用仕様は、Google Drive上のドキュメントにて管理・更新しています。  
      📄 [**YuE2\_Modal\_楽曲自動生成パイプライン\_ロードマップ・運用設計書（Google Drive）**](https://docs.google.com/document/d/1xXgRRyAVwIXKB-dIimDwWwdMl55rxDMn9tvui48THPY/edit)  
    - **Phase 1**: Modal基盤構築 & CLIプロトタイプによる1曲生成検証（✅ 完了）  
    - **Phase 2**: クラウド完結化（Cron定期実行 ＋ Web UI手動トリガー ＋ 歌詞自動最適化）（✅ 完了）  
    - **Phase 3**: Google Drive API 連携による生成音源の自動アップロード（⏳ 次期）  
    - **Phase 4**: 歌詞・スタイルの自動ローテーション生成 ＆ Discord通知連携（📋 計画）  
    - **Phase 5**: ジャケット画像（3000×3000px）・リリック動画生成連携（YouTube / DistroKid）（📋 計画）  
  - **🚀 セットアップと使い方**  
    - **1\. 前提条件**  
      - Python 3.11 または 3.12  
      - [Modal](https://modal.com/) アカウント（月\$30無料枠）  
    - **2\. 環境構築**

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install modal fastapi python-multipart
modal setup
```

    - **3\. クラウドへの常駐デプロイ（Web UI & Cron有効化）**

```sh
modal deploy app.py
```

      - デプロイが完了すると、専用のWeb URLが表示され、スマートフォンやPCブラウザからいつでも楽曲生成を実行できます。  
    - **4\. ローカルCLIからの直接テスト**

```sh
modal run app.py --title "my_track" --style "Japanese, modern J-pop ballad, 92 BPM, piano, strings"
```

    - **5\. クラウドに保存された楽曲の一括ダウンロード**

```sh
modal volume ls yue2-generated-songs
modal volume get yue2-generated-songs / outputs_cloud/
```

├── ROADMAP.md          \# マイルストーン進捗管理

├── .gitignore          \# 生成物・仮想環境の除外設定

└── outputs/            \# ローカルCLI実行時の保存先（自動生成）

---

## 🚀 セットアップと使い方

### 1\. 前提条件

- Python 3.11 または 3.12  
- [Modal](https://modal.com/) アカウント（月\$30無料枠）

### 2\. 環境構築

\# 仮想環境の作成と有効化

python3 \-m venv .venv

source .venv/bin/activate

\# 必須パッケージのインストール

pip install modal fastapi python-multipart

\# Modalアカウント認証

modal setup

### 3\. クラウドへの常駐デプロイ（Web UI & Cron有効化）

modal deploy app.py

デプロイが完了すると、ターミナルに専用のWeb URLが表示されます：

✓ Deployed app yue2-song-generator

├── Function: scheduled\_batch\_generation (Cron: 0 0 \* \* 1\)

└── Web function: web \-\> https\://\<workspace\>--yue2-song-generator-web.modal.run

### 4\. ローカルCLIからの直接テスト

modal run app.py \--title "my\_track" \--style "Japanese, modern J-pop ballad, 92 BPM, piano, strings"

### 5\. クラウドに保存された楽曲の一括ダウンロード

\# 生成曲一覧の確認

modal volume ls yue2-generated-songs

\# ローカルへ一括ダウンロード

modal volume get yue2-generated-songs / outputs\_cloud/

---

## 🔒 コストと安全対策

- **強制タイムアウト**: 推論関数に `timeout=600`（10分）を設定し、万が一のプロセススタックによる過剰課金を防止。  
- **データロスト防止**: 一時コンテナ消滅前に Modal Volume へ即座にコミット。