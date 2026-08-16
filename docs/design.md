[English](en/design.md) | 日本語

# 設計ドキュメント

xangi-searchのアーキテクチャ、検索処理、API、データ保存について説明します。

## 概要

xangi-searchは、ワークスペース内のテキストファイルをローカルで検索する
[xangi](https://github.com/karaage0703/xangi) extensionです。検索自体にLLMは使いません。

```text
User / Agent → xangi → same-origin proxy → xangi-search → Workspace
```

## managed extension構成

`xangi-extension.json`はschema v2の`managed-http` runtimeと`workspace.search` capabilityを
宣言します。xangiがforegroundの`serve` processを起動し、次を管理します。

- ワークスペースのpath
- OSが自動割り当てするlocalhost port
- 実行時だけ使うBearer token
- 起動、停止、状態確認、doctor、更新、失敗時の復元

固定port、PID file、manifest内のURLは使いません。親processがstdinを閉じると子processも
終了します。ブラウザとAIエージェントはxangiのsame-origin proxy経由でアクセスします。

## 検索処理

indexの正本はSQLiteです。標準のHybrid検索は、埋め込みによる意味検索とSQLite FTS5の
キーワード検索を統合します。vector依存がない場合はKeyword検索へ縮退します。

検索結果が不足する場合は、範囲を制限したgrep fallbackを追加します。ディレクトリ重み、
最低関連度、任意の時間減衰を最終scoreへ反映します。埋め込み行列はmemoryへcacheし、queryごとに
SQLiteから全vectorを読み直しません。

起動時と設定された間隔で差分indexを行います。既存indexがある場合は先に検索可能にし、
起動時の更新はバックグラウンドで進めます。

## FACT

FACTはSQLiteを正本とする構造化された事実です。追加、更新、無効化のたびにMarkdown snapshotを
書き出します。既定の出力先は`knowledge/rag_facts.md`です。

出力先はワークスペース相対の`.md`だけを許可します。絶対path、ワークスペース外、`.git/`、
`.xangi-search/`は指定できません。

## データ保存

ワークスペースごとに`.xangi-search/`へindexと設定を保存します。異なるワークスペースを使う
複数のxangi instanceは、index、process、portが分離されます。同じワークスペースを複数instanceで
同時更新する運用は対象外です。

## HTTP API

managed extensionとして起動した場合、全endpointで実行時Bearer tokenが必要です。通常は
xangiのproxyまたはextension APIを経由し、子processのportやtokenを直接扱いません。

| Method | Path | 用途 |
| --- | --- | --- |
| `GET` | `/health` | version、capability、index状態 |
| `GET` | `/ui` | 検索、FACT、設定のWeb UI |
| `GET` | `/search` | ファイル検索。`q`、`mode`、`k`、`s`、`forgetting`、`r2ag`に対応 |
| `GET` | `/file` | ワークスペース内の小さなtext fileを取得 |
| `POST` | `/agent` | xangiのagent backend契約 |
| `GET` / `PUT` | `/settings` | 設定の取得・更新 |
| `POST` | `/reindex` | 差分indexを非同期で開始 |
| `GET` / `POST` | `/facts` | FACTの一覧・追加 |
| `GET` | `/facts/similar` | 類似FACTの検索 |
| `PUT` / `DELETE` | `/facts/{id}` | FACTの更新・無効化 |

通常のservice responseは`schema_version: 1`を使用し、`/agent`はxangi契約の
`schemaVersion: 1`を使用します。`POST /reindex`は受付時にHTTP 202を返し、完了は
`GET /health`で確認します。

## 更新

repository管理版はmanifestの`update.prepare`で`./scripts/prepare-update`を宣言します。
準備programは通常`uv sync --frozen --extra vector`、keyword-only構成では`uv sync --frozen`を
実行します。sourceの切り替え、起動確認、doctor、失敗時のrollbackはxangiが担当します。
