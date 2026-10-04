# YuE2 × Modal 楽曲自動生成パイプライン

オープンソースのAI音楽生成モデル「YuE2（3B）」を、サーバーレスGPUインフラ「Modal」上で稼働させ、定期実行と手動オンデマンド生成をクラウド完結させるパイプラインプロジェクトです。

## 主な特徴
- **サーバーレスGPU**: NVIDIA L4（24GB VRAM）を活用した秒単位課金（常時起動コストゼロ）。
- **完全クラウド完結**:
  - **定期自動実行 (Cron)**: 週次バッチで楽曲を自動ストック。
  - **手動Web UI**: 発行されるURLをブラウザやスマートフォンから開き、ワンタップで生成可能。
- **ABC記譜法（シンボリック・プランニング）連携**: メロディ・コード進行（score.abc）の構造化出力。

## 運用ドキュメント
- [ロードマップ・運用設計書（Google Drive）](https://docs.google.com/document/d/1xXgRRyAVwIXKB-dIimDwWwdMl55rxDMn9tvui48THPY/edit)

## 使い方
### 1. クラウドへ常駐デプロイ（Web UI & Cron有効化）
`modal deploy app.py`
デプロイ後に表示されるURLからWeb UIにアクセスできます。

### 2. ローカルCLIからのテスト実行
`modal run app.py --title "test_song" --style "Japanese, lo-fi hip hop, warm piano"`
