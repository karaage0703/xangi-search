[English](en/usage.md) | 日本語

# 使い方ガイド

xangi-searchの導入、検索、FACT管理、設定方法を説明します。

## xangi extensionとして使う

リポジトリのルートで次のコマンドを実行します。

```bash
uv sync --extra vector
xangi extension link ./xangi-extension.json
xangi extension start xangi-search
xangi extension doctor xangi-search
```

processは先に起動し、埋め込みmodelとindexをバックグラウンドで読み込みます。
`xangi extension start`の成功はHTTPの起動完了を表し、検索準備の完了は
`xangi extension doctor xangi-search`または`GET /health`の`ready: true`で確認します。
準備ができたら、xangiのExtensions画面で`xangi-search`の`Open`を押します。詳しい導入手順は
[セットアップガイド](../XANGI_SETUP.md)を参照してください。

## Web UI

### 検索

`検索`タブへキーワードや探したい内容を入力すると、関連するファイルが表示されます。
検索方式は通常`Hybrid`がおすすめです。

- `Hybrid`: 意味検索とキーワード検索を組み合わせる
- `Vector`: 意味の近さで検索する
- `Keyword`: 文字列の一致を中心に検索する

### FACT

`FACT`タブでは、長く覚えておきたい事実を追加・編集・無効化できます。FACTは通常の
ファイル検索にも利用されます。長いログや文書全体ではなく、1件につき1つの事実へ絞ります。
APIでは`GET /facts/{id}`で1件だけ取得でき、無効化済みFACTも`is_active: 0`として確認できます。

### 設定

`設定`では次の項目をワークスペースごとに変更できます。

- 検索方式、表示件数、最低関連度
- 自動index更新のON/OFFと間隔
- ディレクトリごとの検索重み
- 時間減衰
- FACTのMarkdown書き出し先

設定とindexはワークスペースの`.xangi-search/`に保存されます。起動時と標準30分間隔で、
変更されたファイルだけを再indexします。

## AIエージェントから使う

同梱の[`xs-xangi-search`スキル](../skills/xs-xangi-search/SKILL.md)をワークスペースへ追加すると、
AIエージェントが検索とFACT管理を使うための手順を共有できます。追加先や`AGENTS.md`の変更は、
初回setup時とextension更新後に同梱版とworkspace側を比較し、実質的な差分がある場合だけ提案します。
ユーザーが選択するまでworkspaceは変更しません。

## 単独CLIとして使う

xangiを介さず、コマンドラインからも利用できます。

```bash
# indexを作成
uv run xangi-search --workspace /path/to/workspace index

# 検索
uv run xangi-search --workspace /path/to/workspace search "検索語"

# Web UIを起動（既定: http://127.0.0.1:7891/ui）
uv run xangi-search --workspace /path/to/workspace serve
```

vector依存を使わない軽量構成では、セットアップを`uv sync`にして各コマンドへ
`--no-vector`を付けます。この場合はkeyword検索へ切り替わります。

## トラブルシューティング

### 初回indexが終わらない

`xangi extension doctor xangi-search`を実行し、`initialization_phase`と
`initialization_error`を確認します。低速なCPU、cold cache、大きなindexでは初期化に30秒以上
かかってもprocessは停止されません。準備中の検索requestはHTTP 503と`Retry-After: 2`を返します。
`start`の成功はindex完了を意味しません。

既存indexがある場合は起動時更新中も`ready: true`で検索できます。更新に失敗した場合も既存indexを
使い続け、`degraded: true`と`last_reindex_error`で失敗内容を確認できます。

### 意味検索が使えない

`uv sync --extra vector`を実行してからextensionを再起動します。vector依存がない場合は
自動的にkeyword検索へ切り替わります。

### 検索結果が少ない

検索語を短くする、`Keyword`へ切り替える、最低関連度を下げる、対象ディレクトリの重みを
確認する、の順に試します。

### 設定を初期化したい

設定とindexは`.xangi-search/`にあります。削除すると再作成されますが、indexの再構築が必要です。
必要なFACTや設定を確認してから操作してください。

## xangiホームへの移動

xangi経由で開いた画面の左上に、家アイコン付きの「xangiホーム」ボタンを表示します。Studioと同じ44pxの枠付きボタンで、同じ環境のトップへ移動します。検索・設定操作とは別の共通ナビゲーションとして配置しています。単独起動時は表示しません。

### 起動順の設定

初期化グループ対応xangiでは、既定でgpuグループの他拡張と初期化を直列化する。CPU運用などで不要ならホストのextensions.jsonの対象レコードに `"startup": null` を指定する。複数GPUではexclusiveGroupを分け、モデルの実際のデバイス設定も別途合わせる。ホスト未対応の場合は従来の起動動作となる。

### 読み取り専用で使う

サーバーを起動する環境に `XANGI_SEARCH_READ_ONLY=true` を設定すると、設定変更、FACTの追加・編集・削除、
手動の索引更新をHTTP 403で拒否します。既定は無効です。`GET` と `POST /agent`、起動時・定期的な
差分索引更新は継続します。UIでは設定とFACTを閲覧でき、変更操作は無効になります。
`GET /settings` と `GET /health` の `read_only` で状態を確認できます。APIから解除できません。
変更には起動環境の変更とプロセスの再起動が必要です。この設定はHTTP APIの変更制限であり、
ファイル権限やローカルCLIの書き込み権限を制限するものではありません。

### FACT出力先の移行

出力先は `.xangi-search/` 内のMarkdownに限定され、既定は `.xangi-search/facts.md` です。
生成ファイル以外の既存ファイルやシンボリックリンクは上書きしません。
旧設定がこのディレクトリ外を指す場合、起動時に新しい既定の出力先を使い、他の検索設定は維持します。
設定ファイルへの保存は次の設定更新時、Markdownの書き出しは次のFACT変更時に行います。
SQLiteに保存済みのFACTを引き継ぎ、DBが空なら旧既定ファイルまたは旧設定の出力先から取り込みます。
旧ファイルは変更・削除しません。バックアップ対象に `.xangi-search/facts.md` とSQLiteを含めてください。
