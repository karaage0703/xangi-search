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

managed processはHTTP listenerを先にbindし、既存protocolの`ready` eventを出してから、
埋め込みmodelと`SearchIndex`をbackgroundで初期化します。この`ready`は「HTTPで状態確認
できる」というtransportの準備完了で、検索indexの準備完了とは別です。`GET /health`は
SQLiteを読まず、memory上の状態だけを返します。

- `initialization_phase`: `starting`、`initializing`、`loading_embedder`、
  `loading_index`、`ready`、`error`、`stopping`のいずれか
- `initialization_error`: 初期化失敗の内容。成功時は`null`
- `index_available`: 検索requestを処理できるindexがattach済みか
- `usable_snapshot`: 検索に使える既存snapshotまたは正常に更新したindexがあるか
- `ready`: 利用可能なsnapshotがあり、検索requestを処理できるか

利用可能なsnapshotが未準備の間も`/health`、`/ui`、`/settings`（GET）、`/file`は応答します。
検索、FACT、設定変更、reindexなどindex依存endpointはHTTP 503と`Retry-After: 2`を返すため、
呼び出し側はprocessを落とさず再試行できます。停止が初期化完了と競合した場合は、完成したindexを
attachせずcloseします。
503 responseの`phase`はmodel/index読込中のphaseに加え、初回reindex中を`initial_reindex`、
その失敗を`initial_reindex_failed`として区別します。

既存の利用可能なsnapshotは、起動時refreshの実行中も検索へ使います。refreshが失敗しても
`ready: true`を保ち、`degraded: true`と`last_reindex_error`で更新失敗を示します。新規DBは
最初のreindexが成功するまで`ready: false`です。

shutdown時は新しいreindexを受け付けず、初期化・reindex・自動更新threadの終了を待ってから
SQLiteをcloseします。これにより、初期化完了直後やreindex中の停止でもclose済みconnectionへ
workerが触れません。

## 検索処理

indexの正本はSQLiteです。標準のHybrid検索は、埋め込みによる意味検索とSQLite FTS5の
キーワード検索を統合します。Hybridのbase scoreはkeyword scoreを下限として、vector候補から
漏れた完全一致が意味検索だけの候補より下がらないようにします。vector依存がない場合は
Keyword検索へ縮退します。

固定幅chunkの末尾がoverlap以下なら、その内容は直前chunkにすでに含まれるため独立chunkを
作りません。依存物、build生成物、runtime stateなどのdirectory除外規則はglob patternとして
一か所で宣言し、差分indexとgrep fallbackで共有します。root直下だけを除外するpatternは分けて
扱い、検索経路ごとの条件直書きを避けます。

検索結果が不足する場合は、範囲を制限したgrep fallbackを追加します。ディレクトリ重み、
最低関連度、任意の時間減衰を最終scoreへ反映します。埋め込み行列はmemoryへcacheし、queryごとに
SQLiteから全vectorを読み直しません。

起動時と設定された間隔で差分indexを行います。`SearchIndex`がattachされた時点で既存indexは
検索可能になり、起動時の更新はバックグラウンドで進めます。

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
| `GET` | `/search` | ファイル検索。`q`、`mode`、`k`、`s`、`forgetting`、`r2ag`、`context_chunks`、`context_results`に対応 |
| `GET` | `/file` | ワークスペース内の小さなtext fileを取得 |
| `POST` | `/agent` | xangiのagent backend契約 |
| `GET` / `PUT` | `/settings` | 設定の取得・更新 |
| `POST` | `/reindex` | 差分indexを非同期で開始 |
| `GET` / `POST` | `/facts` | FACTの一覧・追加 |
| `GET` | `/facts/similar` | 類似FACTの検索 |
| `GET` / `PUT` / `DELETE` | `/facts/{id}` | FACTの個別取得・更新・無効化。個別取得は無効化済みFACTも返す |

通常のservice responseは`schema_version: 1`を使用し、`/agent`はxangi契約の
`schemaVersion: 1`を使用します。`POST /reindex`は受付時にHTTP 202を返し、完了は
`GET /health`で確認します。

`context_chunks`は0〜2の整数です。0（既定）は従来どおり各ファイルの最上位チャンクだけを返します。1以上では検索順位を変えず、上位`context_results`件（既定・最大3）の各resultへ、同じファイルの前後チャンクを`context.chunks`として順番に追加します。検索と根拠確認を一往復へ束ねたいagent向けの任意指定で、通常の検索応答は増やしません。

## 更新

repository管理版はmanifestの`update.prepare`で`./scripts/prepare-update`を宣言します。
準備programは通常`uv sync --frozen --extra vector`、keyword-only構成では`uv sync --frozen`を
実行します。sourceの切り替え、起動確認、doctor、失敗時のrollbackはxangiが担当します。
