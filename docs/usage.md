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

初回indexはバックグラウンドで作成されます。`doctor`が成功したら、xangiのExtensions画面で
`xangi-search`の`Open`を押します。詳しい導入手順は
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
ユーザーが選択するまで行いません。

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

`xangi extension doctor xangi-search`を実行し、進捗またはエラーを確認します。`start`の成功は
index完了を意味しません。

### 意味検索が使えない

`uv sync --extra vector`を実行してからextensionを再起動します。vector依存がない場合は
自動的にkeyword検索へ切り替わります。

### 検索結果が少ない

検索語を短くする、`Keyword`へ切り替える、最低関連度を下げる、対象ディレクトリの重みを
確認する、の順に試します。

### 設定を初期化したい

設定とindexは`.xangi-search/`にあります。削除すると再作成されますが、indexの再構築が必要です。
必要なFACTや設定を確認してから操作してください。
