# **YuE2 × Modal 楽曲自動生成パイプライン ロードマップ・運用設計書**

# **1. プロジェクト概要と目標**

* **目的**: オープンソース最先端のAI音楽生成モデル「YuE2（m-a-p/YuE2-3B）」をサーバーレスクラウドGPU基盤「Modal（NVIDIA L4 24GB）」上で稼働させ、ローカルPCのリソースを一切消費せずに「完全自動の定期生成」および「Web UI / スマホからの手動オンデマンド生成」を実現する。  
* **成果物保管**: 生成された音声（FLAC/WAV）、ABC記譜法データ（score.abc）、生成ログ（prompt_info.txt, plan.json）をGoogle Driveへ自動同期し、YouTube動画化や音楽配信（DistroKid）へシームレスに連携する。  
* **コスト方針**: Modalの月額$30無料クレジット枠を活用し、常時稼働費ゼロ（秒単位課金・1曲数円レベル）で経済的かつ持続可能な副業制作パイプラインを確立する。

# **2. システム構成アーキテクチャ**

## **推論・演算層**

* **Modal Serverless GPU**: NVIDIA L4 24GB VRAM（秒単位課金・自動停止）
* **最適化技術**: BF16/INT8推論、PyTorch 2.4+、YuE2公式リポジトリ直接連携
* **歌詞最適化エンジン**: `pykakasi` によるひらがな化・分かち書き・1行5〜8文字改行

## **自律プランニング・通知層**

* **LLM自律作詞エンジン**: Google Gemini API（`gemini-3.8-flash`）による季節・時間帯・ランダム音楽要素に応じたタイトル、プロンプト、歌詞の完全自動生成
* **完了通知**: Discord Webhook 連携による直接試聴リンク・歌詞カード付きリッチ通知

## **ストレージ層**

* **モデルキャッシュ**: Modal Volume（`yue2-model-cache`）にモデル重み（約4GB）を永続化し、高速起動を実現  
* **成果物永続化**: Google Drive API（OAuth 2.0 / `token.json`）経由でマイドライブ（`yue2` フォルダ）へ自動アップロード

## **実行トリガー層**

* **定期自動生成 (Scheduled Cron)**: `modal.Cron` により毎週月曜午前9時 (JST) に完全自動バッチ実行（AI作詞→GPU生成→Drive保存→Discord通知）
* **手動クラウド生成 (Web UI / Webhook)**: Modal FastAPI によるスマホ・ブラウザ対応Web UI（「AIにおまかせ生成」＆「自由入力生成」）
* **開発・検証 (Modal CLI)**: ローカルPCからの高速パラメータ検証（`modal run app.py`）

## **バージョン管理・CI/CD**

* GitHub リポジトリ（ソースコード、README、ROADMAP、Issue/Projects連携）

# **3. マイルストーン別ロードマップと進捗状況**

| フェーズ | マイルストーン名 | 主なタスク・機能 | ステータス |
| :---- | :---- | :---- | :---- |
| **Phase 1** | **基盤構築 & プロトタイプ検証** | ・Modalアカウント連携・仮想環境構築・GPU（NVIDIA L4）動作検証・YuE2-3Bによる1曲目の音声（FLAC）および楽譜（ABC）生成成功・CLI引数対応（--title, --style, --lyrics-file, --seed） | **【完了】** |
| **Phase 2** | **クラウド完結化（Web UI & 歌詞最適化）** | ・Modal Web Endpoint によるクラウドWebフォームの実装・ローカルPCを起動せずにスマホ/ブラウザから生成リクエスト完了・GitHub リポジトリの初期化とコード・設計書の初版コミット・日本語歌詞自動最適化エンジン（pykakasi によるひらがな化・分かち書き・5〜8文字改行）の実装 | **【完了】** |
| **Phase 3** | **ストレージ自動連携（Google Drive）** | ・Google Drive API 認証（OAuth 2.0 / 個人容量利用）のModal Secret設定・クラウド上で生成されたFLAC・ABC楽譜・ログのDrive自動アップロード・スマホからDrive経由で即時試聴できる環境構築 | **【完了】** |
| **Phase 4** | **定期自動実行（Cron） & 通知・AI作詞** | ・Gemini API（`gemini-3.8-flash`）による季節・時間帯に応じた自律作詞・プロンプト生成の実装・`modal.Cron` による完全放置型・定期バッチ生成の高度化・楽曲生成＆Drive保存完了時のDiscord Webhookリッチ通知連携・Web UIへの「AIおまかせ生成」機能の追加 | **【完了】** |
| **Phase 5** | **マルチメディア展開 & 収益化** | ・ジャケット画像（3000×3000px）自動生成連携・リリック動画（MP4）自動レンダリング連携（YouTube_Automation統合）・DistroKidを通じたストリーミング配信申請フローの確立 | **【計画中】** |

# **4. 運用・セキュリティ・コスト管理ルール**

1. **GPU課金防止策**  
   * 全ての関数に `timeout=600`（最大10分）を厳格設定し、スタック時の自動強制終了を担保。
   * LLM作詞（Gemini）やDiscord通知は安価なCPUコンテナ上で完結させ、高価なL4 GPU課金時間を1秒も無駄にしない。
2. **モデル・コンテナ管理**  
   * Modal Volumeによりモデル再ダウンロード時間を排除し、コールドスタートを約30秒〜1分以内に短縮。  
3. **認証情報の保護**  
   * Google Drive認証情報（`google-drive-secret`）、Gemini APIキー（`gemini-secret`）、Discord Webhook（`discord-secret`）はすべて `modal.Secret` に安全に格納し、Gitリポジトリには一切コミットしない。  
4. **月次コスト見積もり**  
   * L4 GPU（$0.80/時）で1曲約2分生成の場合、1曲約$0.027（約4円）。月間約100曲生成しても無料枠$30の範囲内で運用可能。
