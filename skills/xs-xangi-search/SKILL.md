---
name: xs-xangi-search
description: xangi-searchでworkspace内のファイル・過去記録・コードを検索するスキル。「workspaceを検索して」「過去のメモを探して」「この実装があるか調べて」など、回答前にローカル文脈を確認する時に使用。
---

# xangi-search

xangi-search extensionを使い、workspace内の根拠をファイル単位で見つける。

## 実行フロー

### Step 1: extensionの状態を確認する

```bash
xangi extension status xangi-search
```

停止中なら`xangi extension start xangi-search`で起動する。起動中でも`doctor`が失敗する場合は、初回indexの完了または具体的なerrorを確認する。

### Step 2: 検索する

子processのportと認証tokenは親xangiだけが保持する。固定portへ直接`curl`せず、`extension_request`を使う。`--query-json`は日本語や空白を安全にURL encodeする。

```bash
xangi tool extension_request \
  --id xangi-search \
  --capability workspace.search \
  --path /search \
  --query-json '{"q":"検索したい内容","mode":"hybrid","k":8,"s":0.3}'
```

- 通常は`hybrid`
- ファイル名、環境変数、関数名などの正確な文字列は`keyword`
- embeddingが有効だと確認できた場合だけ`vector`

### Step 3: 元ファイルを確認する

検索結果の`file_path`と抜粋だけで事実を断定せず、回答に使うファイルをworkspaceから直接読む。複数候補がある場合は関連度だけでなく、更新時期と記述の具体性も比べる。

`/search`の`facts`は、文書とは別に保存された構造化fact。回答へ使う場合は`source_file`があれば元ファイルも確認し、出典がないfactは保存済みメモとして扱う。

### Step 4: 0件を切り分ける

0件または明らかに少ない場合は、検索語を固有名詞や識別子へ短くして1回再検索する。`hybrid`で見つからない正確な語は`keyword`も試す。それでも見つからない場合は、試したqueryとmodeを添えて「確認した範囲では見つからない」と報告する。

### Step 5: 必要な時だけ再indexする

直近に追加・更新されたファイルが検索へ出ない場合は再indexを要求する。

```bash
xangi tool extension_request \
  --id xangi-search \
  --capability workspace.search \
  --path /reindex \
  --method POST
```

HTTP 202は受付であり完了ではない。次の方法で`/health`を確認し、`reindex_in_progress`が`false`、`last_reindex_error`が空になってから再検索する。

```bash
xangi tool extension_request \
  --id xangi-search \
  --capability workspace.search \
  --path /health
```

## factを管理する

ユーザーが「覚えて」「記録して」「前の内容を更新して」「このfactを消して」と依頼した時、またはworkspaceで継続利用する重要な事実が確定した時に使う。長い進捗ログ、推測、一時的な値、秘密情報はfactへ入れない。

操作前に必ず類似factを検索する。

```bash
xangi tool extension_request \
  --id xangi-search \
  --capability workspace.search \
  --path /facts/similar \
  --query-json '{"q":"記録したい事実","k":3}'
```

判定は次の通り。

- ADD: 別トピックの新しい事実。`POST /facts`
- UPDATE: 同じ事実の内容が変わった。既存IDへ`PUT /facts/{id}`
- DELETE: 誤り、撤回、保存不要になった。既存IDへ`DELETE /facts/{id}`
- skip: 同じ内容が既にある、推測しかない、長いログや秘密情報

```bash
# ADD
xangi tool extension_request \
  --id xangi-search --capability workspace.search \
  --path /facts --method POST \
  --body-json '{"facts":[{"text":"1件1事実","source_file":"notes/example.md","fact_date":"2026-08-16"}]}'

# UPDATE
xangi tool extension_request \
  --id xangi-search --capability workspace.search \
  --path /facts/123 --method PUT \
  --body-json '{"text":"更新後の1件1事実"}'

# DELETE
xangi tool extension_request \
  --id xangi-search --capability workspace.search \
  --path /facts/123 --method DELETE
```

ADD/UPDATEではresponseの`result`または`results`にあるIDとtextを確認する。DELETEは`result.is_active`が`0`であることを確認し、最後に`GET /facts`で反映を確認する。失敗した場合は別IDへ重複登録せず、method・path・responseを報告する。

## 結果の提示

結論の近くに根拠となるworkspace相対pathを示す。検索自体を依頼された場合は、検索語、見つかったファイル、要点を箇条書きで返す。

```text
「M5Stack」で検索し、3ファイルを確認しました。

- notes/example.md: 実機構成の記録
- src/device.ts: 接続処理の実装
```

## Gotchas / よくある失敗

- extensionリポジトリを検索対象workspaceと取り違えない。対象workspaceはextension lifecycleから渡される。
- 202応答だけで再index完了としない。`/health`を確認する。
- 抜粋やタイトルだけで断定せず、根拠ファイルを読む。
- 固定portへの直接`curl`や認証tokenの取得を試みない。`extension_request`を使う。
- fact追加前の類似検索を省略しない。同じ事実の更新を別IDへADDしない。

## 使用例

```text
workspaceから過去のM5Stack検証を検索して
この環境変数を使っている実装があるか調べて
外部仕様と過去のローカル実測を突き合わせて
```

## 完了前チェックリスト

- [ ] 検索結果の元ファイルを確認した
- [ ] 0件または少数結果ならqueryまたはmodeを変えて再確認した
- [ ] 結論に必要なworkspace相対pathを示した
- [ ] factを変更した場合は類似検索、ADD/UPDATE/DELETE判断、反映確認を行った
