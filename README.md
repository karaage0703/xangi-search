[English](README.en.md) | 日本語

# xangi-search

[xangi](https://github.com/karaage0703/xangi)のワークスペースを、ローカルで検索するextensionです。
ファイルの全文検索と意味検索を組み合わせ、過去のメモや記録をすばやく見つけます。

## 主な機能

- ワークスペース内の関連ファイルを検索
- 覚えておきたい事実をFACTとして保存・検索
- Web UIから検索条件や自動index更新を設定
- xangiが起動・停止・更新を管理

検索indexと設定はワークスペース内の`.xangi-search/`に保存されます。
検索処理はローカルで完結し、LLMは使用しません。

## セットアップ

リポジトリを取得し、次のコマンドを実行します。

```bash
uv sync --extra vector
xangi extension link ./xangi-extension.json
xangi extension start xangi-search
xangi extension doctor xangi-search
```

初回はindex作成に時間がかかることがあります。`doctor`が成功したら、xangiの
Extensions画面で`xangi-search`の`Open`を押してください。

詳しい手順と軽量なkeyword-only構成は[XANGI_SETUP.md](XANGI_SETUP.md)を参照してください。

## 使い方

- `検索`: 探したいキーワードや内容を入力します。
- `FACT`: 長く覚えておきたい事実を追加・編集します。
- `設定`: 検索方式、表示件数、対象ディレクトリの重み、自動index更新などを変更します。

AIエージェントから利用する場合は、同梱の
[`xs-xangi-search`スキル](skills/xs-xangi-search/SKILL.md)も追加できます。

## ドキュメント

- [使い方ガイド](docs/usage.md) - UI、CLI、FACT、設定、トラブルシューティング
- [設計ドキュメント](docs/design.md) - アーキテクチャ、検索処理、API、データ保存
- [セットアップガイド](XANGI_SETUP.md) - xangi extensionとして導入する手順

## 開発

```bash
uv sync --extra vector
uv run pytest
```
