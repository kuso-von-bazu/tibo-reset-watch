# Tibo Reset Watch

Tiboさん（[@thsottiaux](https://x.com/thsottiaux)）のCodex / ChatGPT Workのリセット予告や示唆を拾い、**GitHub IssueとDiscord**へ日本語で通知します。Pythonの標準ライブラリだけで動き、**監視時のLLM呼び出し・AIトークン消費はゼロ**です。

このリポジトリをGitHubに置けば、Codexや手元のPCを開いていなくてもGitHub Actionsが定期実行します。ChatGPTの定期タスクとは別のプログラムです。このチャットへの直接通知機能はありません。

## 取得方法と費用

| モード | 取得元 | 設定 | 注意点 |
| --- | --- | --- | --- |
| `tracker`（初期設定） | Reset Beaconの公開証拠API | Xのキー不要 | 第三者が保存した本人投稿を確認。保存されない投稿・返信の予兆は拾えません。サービスの停止・遅延・仕様変更の影響を受けます。 |
| `x` | X API v2の本人タイムライン | `X_BEARER_TOKEN` | 返信と取得できた返信先も確認。X APIの利用権限・料金・制限はX側の条件によります。 |

trackerモードは自分のX APIキーもAI APIキーも不要です。GitHub Actionsの実行枠と通知先の料金条件は別です。AIを呼ばないルール判定なので、遠回しな言い方・画像だけの示唆を完全には理解できません。「雰囲気」の精度を優先したい場合はAIによる監視と違いが出ます。

初期設定は**1時間ごと**（毎時17分）です。GitHub Actionsの実行は遅れることがあり、即時性は保証されません。

## GitHubに設置する

1. GitHubで自分用のリポジトリを作り、このフォルダの中身を入れます。隠しフォルダの **`.github/workflows`** も含めてください。Forkできる公開リポジトリにする場合も同じ構成です。
2. リポジトリのIssuesを有効にします。**Watch → Custom → Issues**など、必要なGitHub通知を設定します。通知先のメール・アプリ設定はGitHubアカウント側で設定します。
3. Discordの通知先チャンネルでWebhookを作ります。GitHubの **Settings → Secrets and variables → Actions → New repository secret** に、名前 **`DISCORD_WEBHOOK_URL`** でURLを保存します。ソースコードにURLを入れないでください。
4. **Actions → Tibo reset alerts → Run workflow** の`mode`を **`check-config`** にして実行します。これは取得元・通知先を読み取りだけで確認し、通知や状態の保存はしません。成功したことをActionsのログで確認します。
5. `mode`を **`test-notification`** にして手動実行し、GitHub IssueとDiscordに「接続テスト」が届くことを確認します。実際のリセット告知とは区別され、監視の記録を進めません。
6. `mode`を **`watch`** にして初回の監視を実行します。初回は既存投稿を記録し、古い告知を通知しません。以後の新しい投稿から通知します。初回にも直近48時間の告知を受け取りたい場合は、手動実行時に **「初回にも直近48時間の投稿を通知する」** を選びます。

Actionsの初期設定では**GitHubとDiscordの両方が有効**です。DiscordのWebhookが未設定なら、投稿取得前に設定エラーとして停止します。GitHubだけで使いたい場合は、ActionsのRepository variableに`NOTIFY_DISCORD=false`を登録します。Webhookを削除するだけではGitHubのみの監視に切り替わりません。

`GITHUB_TOKEN`はActionsが自動で用意します。別途PATは不要です。ワークフローはIssuesへの書き込みと、状態保存用ブランチへの書き込み権限を必要とします。組織のポリシーで制限されている場合は、このワークフローに対応する設定が必要です。

スケジュール実行はデフォルトブランチ上のワークフローで動きます。公開リポジトリで活動が60日ない場合、GitHubはスケジュールを無効化することがあります。実行状況はActionsで確認してください。Fork先ではActionsの有効化が必要になる場合があります。

### GitHub CLIで設置する

`deploy.py`は、コードの配置、Actionsの変数・Secretsの設定、読み取りだけの接続確認の起動をまとめて実行します。GitHub CLI（`gh`）の認証と、設置先へのコード・ワークフロー・Secretsの書き込み権限が必要です。GitHub通知用の実行時トークンは、設置後もActionsが用意します。

操作内容を通信なしで確認します。`YOUR_ACCOUNT`は自分のアカウントに置き換えてください。

```bash
python deploy.py --repo YOUR_ACCOUNT/tibo-reset-watch --plan
```

既存リポジトリへ配置する場合：

```bash
gh auth login
python deploy.py --repo YOUR_ACCOUNT/tibo-reset-watch
```

DiscordのWebhook URLは非表示の入力で受け取り、`gh secret set`の標準入力へ渡します。コード・コマンド引数・ログに保存しません。Xモードを選ぶ場合は、XのBearer Tokenも同じ方法で登録します。すでにGitHub Secretsへ登録してある場合は、値を取り出さず登録名を確認して使えます。

```bash
python deploy.py --repo YOUR_ACCOUNT/tibo-reset-watch --use-existing-secrets
```

新規作成する場合は公開範囲を明示します。

```bash
python deploy.py --repo YOUR_ACCOUNT/tibo-reset-watch --create private
```

公開共有を選ぶ場合は`--create public`を使います。既存の別ファイルは残し、このパッケージのファイルだけをGitコミットで配置します。同名ファイルの内容が違う場合は停止します。差分を確認し、意図した更新なら`--update`を指定してください。コードの強制pushはしません。

接続確認の起動成功は、通知成功やActionsの実行完了を意味しません。表示されたActionsページで結果を確認し、`test-notification`、`watch`の順に実行します。コード配置と同時に毎時のスケジュールも登録されます。途中で設定に失敗した場合は表示された段階を確認し、Secretsを整えて再実行してください。

### 本人のXを直接チェックしたい場合

1. X Developer Consoleで、ユーザー投稿の読み取りを利用できるBearer Tokenを用意します。Xへのログインパスワードではありません。
2. ActionsのRepository secretに **`X_BEARER_TOKEN`** を保存します。
3. ActionsのRepository variableに **`SOURCE` = `x`** を保存します。
4. 取得元ごとに状態を分けるため、既にtrackerモードを稼働させた場合は、Actions実行中でないことを確認して `tibo-watch-state` ブランチを削除し、手動実行して新しい基準を作ります。

Xモードは`since_id`以降だけを取得して読み取りを抑えます。本人IDの照会は初回だけ、最大100件ずつページを取得します。リポストを除外し、返信を含めます。親投稿の取得ができない短い返信は、文脈を推測して確定しません。取得制限やエラー時は処理を失敗させ、未処理投稿のチェックポイントを進めません。

## ローカルで試す

Python 3.11以上。追加パッケージは不要です。

```bash
python watch.py --dry-run
python -m unittest discover -s tests -v
```

`--dry-run`は通知・状態の保存をせず、直近48時間の検知候補を表示します。公開APIから取得するためネットワーク接続は必要です。通信なしの動作例は次のコマンドで確認できます。

```bash
python watch.py --fixture examples/posts.json --dry-run
```

サンプルは**架空の動作確認用データ**です。日時が48時間以上前になったら表示対象から外れます。テストは日時を固定して検証するため、いつでも実行できます。

通知を稼働させる場合は、実行環境に`DISCORD_WEBHOOK_URL`を渡して `python watch.py`を実行します。環境変数は自動で`.env`から読み込みません。GitHub通知には`NOTIFY_GITHUB=true`、`GITHUB_REPOSITORY=owner/repo`、権限のある`GITHUB_TOKEN`も必要です。ローカル状態は`.tibo-watch/state.json`に保存します。cronや別のスケジューラーから起動でき、AIは使いません。複数プロセスから同じ状態ファイルを同時に更新しないでください。

接続確認とテスト送信は、環境変数を設定した状態で次のコマンドからも実行できます。

```bash
python watch.py --check-config
python watch.py --test-notification
```

`--check-config`は読み取りだけで基本的な接続を確認します。書き込みや配送の成否は、明示的に送る`--test-notification`で確認します。ローカルでも両方を必須にする場合は`NOTIFY_GITHUB=true`と`NOTIFY_DISCORD=true`を設定します。

## 通知内容と判定

- **予告あり**：リセットや券の付与予定が本人の文言にある。実施完了は未確認。
- **予兆・未確定**：利用枠を使うよう促す表現、投票や条件付きのリセット言及、質問への肯定的な返信など。配布保証はありません。
- **実施／配布告知**：本人がリセットの実施済み・反映済みを述べた。個別アカウントへの反映は未確認。

banked reset（任意に使う券）は利用枠の一斉リセットと区別します。本人の投稿ID、内容のハッシュ、通知先ごとの送信成功を記録して、同じ投稿の繰り返し通知を抑えます。投稿内容が編集された場合は再判定し、検知対象なら更新として通知します。別IDに同じ話題が再投稿された場合は別の通知になります。

対象プラン・配布時刻などをルールから推測しません。本人の元投稿リンク、投稿日（日本時間とUTC）、短い原文を添えます。転載で確認した場合は証拠ページへのリンクも添えます。障害の謝罪・新モデル発表・第三者のリセット要求だけでは通知しません。取得失敗は「予定なし」と判定せず、Actionsの実行失敗として表示します。

## 重複防止と停止

Actionsでは `tibo-watch-state` ブランチの `.tibo-watch/state.json` に状態を保存します。Actionsキャッシュの消失に依存せず、並行実行を抑えます。メインのソースコードブランチは状態保存で更新しません。各通知先への成功後に保存するため、Discordが失敗してもGitHubへの成功分を再送せず、次回に失敗分を再試行します。

送信成功と状態保存の間に停止した場合など、完全な一度だけの配送は保証できません。GitHubは投稿・内容ごとの識別子を既存Issueからも照合しますが、検索の反映には遅延があり得ます。Discordは状態保存前の停止や応答不明の場合、重複する可能性があります。失敗した投稿は48時間の検知対象期間内で再試行されます。

trackerモードは直近の保存投稿100件まで確認します。APIが本人の全投稿を保存していることや、欠落がないことは検証できません。未登録の投稿や100件を超える保存投稿の間の欠落は見逃す可能性があります。

停止するにはGitHubの **Actions → Tibo reset alerts → Disable workflow** を選びます。Discordだけ止める場合はRepository variableの`NOTIFY_DISCORD`を`false`にします。このプログラムを停止しても、ChatGPTに作成済みの定期タスクは別途動いています。AIのトークン消費を止めるには、そのタスクも停止してください。

## 参照

- [Reset Beaconの公開API仕様](https://resetbeacon.com/api/docs/)（非公式の第三者サービス）
- [X API：ユーザーの投稿取得](https://docs.x.com/x-api/users/get-posts)
- [GitHub Actions：スケジュール実行の制約](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)

MIT License。OpenAI、X、Reset Beaconとは提携していません。
