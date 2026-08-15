# xangi-search

xangiから独立して動く、ローカルworkspace向け検索serviceです。

## API

- `GET /health`: version、capability、index状態
- `GET /ui`: 設定とインクリメンタル検索を備えた単独Web UI
- `GET /search?q=...&mode=hybrid&k=8&s=0.3&forgetting=off`: file単位の検索結果。`r2ag=on`にも対応
- `POST /agent`: xangiの汎用extension backend契約。検索語の整形と回答Markdown生成もこのserviceが担当
- `GET /settings` / `PUT /settings`: 検索・自動index設定
- `POST /reindex`: 差分indexを非同期更新（受付はHTTP 202）
- `GET /facts` / `GET /facts/similar?q=...`: 構造化factの一覧・類似検索
- `POST /facts`、`PUT /facts/{id}`、`DELETE /facts/{id}`: factの追加・更新・無効化

responseには`schema_version: 1`を含めます。xangiとの結合は
`xangi-extension.json`に宣言した`workspace.search` capability経由で行います。
Web UIから追加する場合の手順は`XANGI_SETUP.md`に置き、manifestの
`setup.instructions`から参照します。利用スキル`skills/xs-xangi-search/SKILL.md`も
同梱し、セットアップ時にworkspaceへの追加と`AGENTS.md`の最小ルールを提案します。
ユーザーが選択するまではworkspaceのファイルを変更しません。

extension entrypointは`start`、`stop`、`restart`、`status`、`doctor`、`update`
という共通actionをJSONで受け渡します。初版の`update`はcontractだけを予約し、
checkoutはGit、将来の配布版は署名済みpackage updaterが所有します。

## 開発

```bash
uv sync --extra vector
uv run pytest
uv run xangi-search --workspace /path/to/workspace index
uv run xangi-search --workspace /path/to/workspace serve
uv run xangi-search --workspace /path/to/workspace evaluate --queries queries.jsonl --mode hybrid --k 5
```

標準構成は`uv sync --extra vector`でembeddingを有効にし、hybrid検索を使います。vector依存が
ない環境は自動的にkeyword検索へ縮退します。軽量構成を明示する場合は`--no-vector`を指定します。
既定portは`7891`です。extension lifecycleから一時的にkeyword-onlyで起動する場合は
`XANGI_SEARCH_NO_VECTOR=true`を使えます。

評価queryは1行1 JSONで、`{"query":"検索語","relevant":["正解/file.md"]}`の形式です。
出力はRecall@k、MRR@k、p95 latencyとquery別の順位を含みます。
`serve`は既存indexをすぐ利用可能にし、起動時の差分indexをバックグラウンドで行います。
標準では30分ごとに変更ファイルだけを再indexします。UIまたは`PUT /settings`で自動更新の
ON/OFF、間隔、検索方式、表示件数、最低関連度、ディレクトリごとの重み、時間減衰を
workspaceごとに変更できます。設定ファイルがない場合は、現在のworkspaceに実在する
index対象のトップレベルディレクトリだけを、名前によらず一律1.0で表示します。
特定workspace固有の重みは自動推測せず、UIまたは`PUT /settings`で明示的に設定します。手動設定した相対pathが
存在しなくなった場合はUIで警告し、勝手に削除はしません。より長く一致する相対pathを
優先します。設定とindexはworkspaceの`.xangi-search/`に保存されます。

xangiのExtensions画面からUIを開いた場合は、同じoriginのxangiトップへ戻るリンクを
表示します。serviceの`/ui`を直接開いた場合は、存在しないxangi URLを推測せずリンクを
表示しません。

検索対象のtext形式・除外規則、grep fallback、R²AG形式、fact統合、任意の忘却曲線は
workspace-RAG互換です。embedding行列は起動・差分index完了時にmemoryへcacheし、queryごとに
SQLiteから全vectorを読み直しません。`/search`は全体の`timings_ms`と検索内訳の
`rag_timings_ms`を返します。
