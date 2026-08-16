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

## 利用スキルを提案する

extensionの確認後、workspace内の既存構成を読み取り、同梱スキル`skills/xs-xangi-search/SKILL.md`の追加を提案します。

1. workspaceに`skills/`、`.agents/skills/`、`.claude/skills/`のどれがあるか確認し、既存の配置規則を優先します。規則がなければ`skills/xs-xangi-search/`を提案します。
2. 同名スキルがある場合は上書きせず、現在の内容との差分を示します。
3. `AGENTS.md`がある場合は内容を読み、次のような最小ルールの追記案を示します。既存の指示と重複する項目は追加しません。

```markdown
## xangi-search

- workspace内の過去記録やファイルを参照して答える時は、`xs-xangi-search`スキルを使う。
- 外部情報を取得した後、ローカル文脈が関係する場合は、同じトピックでworkspaceを再検索する。
- 0件だけで「記録なし」と断定せず、検索語を短くするかkeyword modeで1回再検索する。
- 覚えておくべき永続的な事実は、類似factを検索してからADD・UPDATE・DELETEを判断する。
```

提案時は、ユーザーへ次の選択肢を示します。

- 推奨: スキル追加 + `AGENTS.md`への最小ルール追記
- スキルだけ追加
- workspaceは変更せず、extensionだけ利用

選択されるまではworkspaceのスキルや`AGENTS.md`を変更しません。変更後は追加・更新したpathと差分を報告します。ユーザー向けの説明は会話で使われている言語に合わせます。

## fact利用を確認する

スキルを追加した場合は固定portへ直接接続せず、親xangiの`extension_request`経由で次を確認します。

1. `GET /facts/similar?q=...&k=3`で既存factを検索できる。
2. テスト用factを`POST /facts`で追加し、返されたIDを記録する。
3. 同じIDを`PUT /facts/{id}`で更新し、`GET /facts`へ反映される。
4. 同じIDを`DELETE /facts/{id}`で無効化し、`GET /facts`で`is_active: 0`になる。
5. extensionを再起動し、通常のfactが保持されることを確認する。テスト用factは確認後に無効化する。

factは長いログや文書の代わりではありません。1件につき1つの長期的に参照したい事実へ絞り、秘密情報を保存せず、可能なら`source_file`と`fact_date`を付けます。追加前に類似検索し、同じ事実の変更は新規ADDではなく既存IDのUPDATEを使います。子processのportや認証tokenは取得・記録しません。
