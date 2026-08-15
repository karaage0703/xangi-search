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

停止中なら`xangi extension start xangi-search`で起動する。statusが返す`healthUrl`を正本とし、以下の例とportが異なる場合はそのURLを使う。

### Step 2: 検索する

日本語や空白を含むqueryはURLへ直書きせず、`--data-urlencode`で渡す。

```bash
curl --connect-timeout 1 --max-time 5 -sS -G \
  'http://127.0.0.1:7891/search' \
  --data-urlencode 'q=検索したい内容' \
  --data-urlencode 'mode=hybrid' \
  --data-urlencode 'k=8' \
  --data-urlencode 's=0.3'
```

- 通常は`hybrid`
- ファイル名、環境変数、関数名などの正確な文字列は`keyword`
- embeddingが有効だと確認できた場合だけ`vector`

### Step 3: 元ファイルを確認する

検索結果の`file_path`と抜粋だけで事実を断定せず、回答に使うファイルをworkspaceから直接読む。複数候補がある場合は関連度だけでなく、更新時期と記述の具体性も比べる。

### Step 4: 0件を切り分ける

0件または明らかに少ない場合は、検索語を固有名詞や識別子へ短くして1回再検索する。`hybrid`で見つからない正確な語は`keyword`も試す。それでも見つからない場合は、試したqueryとmodeを添えて「確認した範囲では見つからない」と報告する。

### Step 5: 必要な時だけ再indexする

直近に追加・更新されたファイルが検索へ出ない場合は再indexを要求する。

```bash
curl --connect-timeout 1 --max-time 5 -sS -X POST \
  'http://127.0.0.1:7891/reindex'
```

HTTP 202は受付であり完了ではない。`/health`の`reindex_in_progress`が`false`になり、`last_reindex_error`が空であることを確認してから再検索する。

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
