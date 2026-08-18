[English](XANGI_SETUP.en.md) | 日本語

# xangi-search セットアップ

このリポジトリはローカルで動くxangi extensionです。リポジトリルートでセットアップしてください。

## extensionをセットアップする

1. `uv`と`xangi`が利用可能か確認します。system packageの導入や`sudo`は自動実行しません。
2. `uv sync --extra vector`でローカル環境を作ります。標準構成はembedding検索とkeyword検索を組み合わせたhybrid検索です。ユーザーが明示的に軽量構成を希望した場合、または追加のモデル依存を扱えない環境だけ、`uv sync`と`XANGI_SEARCH_NO_VECTOR=true`を使ってkeyword-onlyにします。
3. `xangi extension link ./xangi-extension.json`でこのcheckoutを登録します。
4. このsetup会話を実行しているxangi instanceを対象に`xangi extension start xangi-search`で起動します。別instanceを推測して接続しません。xangiのworkspaceは親runtime managerから渡されるため、extensionリポジトリのpathへ置き換えません。
5. `xangi extension list`で`xangi-search`が`autostart`として登録されたことを確認します。
6. `xangi extension status xangi-search`でservice起動を確認し、`xangi extension doctor xangi-search`が成功するまで初回indexの進捗を確認します。初回indexはバックグラウンドで動くため、`start`の成功だけでindex完了とみなしません。
7. xangiのExtensions画面から`Open`を押して単独UIを開き、検索語を入力して複数結果が表示されること、実在ディレクトリの重みを含む設定が再読込後も維持されること、手動indexの状態が画面へ反映されること、`xangiへ戻る`で同じ環境のトップへ戻れることを確認します。service単体では`/ui`でも開けますが、xangiのURLを推測できないため戻るリンクは表示しません。

検索index、任意のembedding、検索・自動index設定はworkspaceの`.xangi-search/`に保存するextension所有stateです。子processのportと認証tokenは親xangiが実行時だけ保持します。workspaceが異なる複数xangi instanceは互いに分離されます。同じworkspaceを複数instanceから同時更新する設定は行いません。既存indexはセットアップ時に削除しません。起動時と標準30分間隔で差分indexを行います。xangi scheduleやOSのcronへindex処理を重複登録しません。コマンドが失敗した場合は、設定を変更する前に実行したコマンドとエラーをそのまま報告します。

## 回答前の事前検索を提案する

xangiが`UserPromptSubmit` hookと`extension_request --query-json-stdin`に対応している場合、ユーザー入力をLLMへ渡す前にxangi-searchで1回だけ検索し、12,000文字以内の根拠候補を追加できます。これはxangiの汎用command hookを使う設定であり、xangi-search専用の処理をxangi本体へ追加しません。

1. `xangi tool help extension_request`に`--query-json-stdin`が表示されることを確認します。未対応ならhookを設定せず、必要なxangi versionを報告します。
2. リポジトリ内の`.venv/bin/xangi-search-preflight-hook`が実行可能であることを確認します。
3. workspaceの`hooks/hooks.json`を読み、既存の`Stop`や他の`UserPromptSubmit` hookを保持したまま、次の追加差分をユーザーへ提示します。`<absolute-repository-path>`は現在のcheckoutの絶対pathへ置換します。

```json
{
  "hooks": {
    "UserPromptSubmit": [
      {
        "id": "xangi-search-preflight",
        "exec": {
          "file": "<absolute-repository-path>/.venv/bin/xangi-search-preflight-hook",
          "args": []
        },
        "timeoutMs": 10000,
        "maxOutputChars": 14000
      }
    ]
  }
}
```

このhookはwrapper展開前の現在のユーザー入力だけをstdin JSONで受け取り、親xangiの`extension_request`経由で`/search`を呼びます。会話履歴、system prompt、環境変数、認証tokenをxangi-searchへ渡しません。検索失敗・timeout・0件時は追加contextなしで通常応答を続けます。

設定前に、永続的に毎turn実行されること、実行file、渡すデータ、10秒timeout、14,000文字の出力上限を明示し、次の選択肢を示します。

- 推奨: 事前検索hookを追加
- extensionとスキルだけ利用し、hookは追加しない

選択されるまで`hooks/hooks.json`を変更しません。同じ`id`が既にある場合は重複追加せず、実行fileや上限が異なれば差分を示して再確認します。extensionを停止・削除する場合は、このhookも無効化または削除するか確認します。

## 利用スキルの追加・更新を提案する

初回setupまたはextension更新の確認後、workspace内の既存構成を読み取り、同梱スキル`skills/xs-xangi-search/SKILL.md`の追加または更新を必要な場合だけ提案します。

1. workspaceに`skills/`、`.agents/skills/`、`.claude/skills/`のどれがあるか確認し、既存の配置規則を優先します。規則がなければ`skills/xs-xangi-search/`を提案します。
2. 同名スキルがある場合は同梱版と比較します。API、操作手順、失敗時の扱いなどに実質的な差分がある場合だけ、理由、対象path、変更概要を示して更新を提案します。表記や整形だけの差分、またはworkspace側が同等以上の手順を持つ場合は提案しません。
3. `AGENTS.md`がある場合は内容を読み、常時適用するルールに不足や古い記述がある時だけ、次のような最小変更を提案します。既存の指示やユーザー固有ルールを置換せず、重複する項目は追加しません。

```markdown
## xangi-search

- workspace内の過去記録やファイルを参照して答える時は、`xs-xangi-search`スキルを使う。
- 外部情報を取得した後、ローカル文脈が関係する場合は、同じトピックでworkspaceを再検索する。
- 0件だけで「記録なし」と断定せず、検索語を短くするかkeyword modeで1回再検索する。
- 覚えておくべき永続的な事実は、類似factを検索してからADD・UPDATE・DELETEを判断する。
```

追加または更新の提案が必要な時は、ユーザーへ次の選択肢を示します。

- 推奨: スキル追加・更新 + `AGENTS.md`への必要最小限の変更
- スキルだけ追加・更新
- workspaceは変更せず、extensionだけ利用

extensionのsetupや更新を承認したことはworkspace変更の承認を兼ねません。選択されるまではworkspaceのスキルや`AGENTS.md`を変更しません。変更後は追加・更新したpath、差分、確認結果を報告します。更新不要ならworkspace変更の提案なしと明記します。ユーザー向けの説明は会話で使われている言語に合わせます。

## fact利用を確認する

スキルを追加した場合は固定portへ直接接続せず、親xangiの`extension_request`経由で次を確認します。

1. `GET /facts/similar?q=...&k=3`で既存factを検索できる。
2. テスト用factを`POST /facts`で追加し、返されたIDを記録する。
3. 同じIDを`PUT /facts/{id}`で更新し、`GET /facts/{id}`へ反映される。
4. 同じIDを`DELETE /facts/{id}`で無効化し、`GET /facts/{id}`で`is_active: 0`になる。
5. extensionを再起動し、通常のfactが保持されることを確認する。テスト用factは確認後に無効化する。

factは長いログや文書の代わりではありません。1件につき1つの長期的に参照したい事実へ絞り、秘密情報を保存せず、可能なら`source_file`と`fact_date`を付けます。追加前に類似検索し、同じ事実の変更は新規ADDではなく既存IDのUPDATEを使います。子processのportや認証tokenは取得・記録しません。
