[English](XANGI_SETUP.en.md) | 日本語

# xangi-search セットアップ

このリポジトリはローカルで動くxangi extensionです。リポジトリルートでセットアップしてください。

## extensionをセットアップする

1. `uv`と`xangi`が利用可能か確認します。system packageの導入や`sudo`は自動実行しません。
2. `uv sync --extra vector`でローカル環境を作ります。標準構成はembedding検索とkeyword検索を組み合わせたhybrid検索です。ユーザーが明示的に軽量構成を希望した場合、または追加のモデル依存を扱えない環境だけ、`uv sync`と`XANGI_SEARCH_NO_VECTOR=true`を使ってkeyword-onlyにします。
3. `xangi extension link ./xangi-extension.json`でこのcheckoutを登録します。
4. `xangi extension start xangi-search`で起動します。xangiのworkspaceはextension lifecycleから渡されるため、extensionリポジトリのpathへ置き換えません。
5. `xangi extension list`で`xangi-search`が`autostart`として登録されたことを確認します。
6. `xangi extension status xangi-search`でservice起動を確認し、`xangi extension doctor xangi-search`が成功するまで初回indexの進捗を確認します。初回indexはバックグラウンドで動くため、`start`の成功だけでindex完了とみなしません。
7. xangiのExtensions画面から`Open`を押して単独UIを開き、検索語を入力して複数結果が表示されること、実在ディレクトリの重みを含む設定が再読込後も維持されること、手動indexの状態が画面へ反映されること、`xangiへ戻る`で同じ環境のトップへ戻れることを確認します。service単体では`/ui`でも開けますが、xangiのURLを推測できないため戻るリンクは表示しません。

検索index、任意のembedding、検索・自動index設定はextensionが所有するstateです。既存indexはセットアップ時に削除しません。起動時と標準30分間隔で差分indexを行います。xangi scheduleやOSのcronへindex処理を重複登録しません。コマンドが失敗した場合は、設定を変更する前に実行したコマンドとエラーをそのまま報告します。

## 利用スキルを提案する

extensionの確認後、workspace内の既存構成を読み取り、同梱スキル`skills/xs-xangi-search/SKILL.md`の追加を提案します。

1. workspaceに`skills/`、`.agents/skills/`、`.claude/skills/`のどれがあるか確認し、既存の配置規則を優先します。規則がなければ`skills/xs-xangi-search/`を提案します。
2. 同名スキルがある場合は上書きせず、現在の内容との差分を示します。
3. `AGENTS.md`がある場合は内容を読み、次のような最小ルールの追記案を示します。既存の指示と重複する項目は追加しません。

```markdown
## xangi search

- workspace内の過去記録やファイルを参照して答える時は、`xs-xangi-search`スキルを使う。
- 外部情報を取得した後、ローカル文脈が関係する場合は、同じトピックでworkspaceを再検索する。
- 0件だけで「記録なし」と断定せず、検索語を短くするかkeyword modeで1回再検索する。
```

提案時は、ユーザーへ次の選択肢を示します。

- 推奨: スキル追加 + `AGENTS.md`への最小ルール追記
- スキルだけ追加
- workspaceは変更せず、extensionだけ利用

選択されるまではworkspaceのスキルや`AGENTS.md`を変更しません。変更後は追加・更新したpathと差分を報告します。ユーザー向けの説明は会話で使われている言語に合わせます。
