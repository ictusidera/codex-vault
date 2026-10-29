# Codex Vault 1.0.5

1.0.5: Codexが生成するarg0一時ヘルパーを取得対象から外し、Windowsで読み取り専用ファイルを含む一時コピーを削除できるよう修正しました。既存の設定・認証情報・バックアップ先は継続利用できます。[変更履歴](docs/CHANGELOG.md)

Codexのローカル会話・状態DB・設定と、開発プロジェクトをresticへ暗号化して世代保存するツールです。Windowsでの日常運用を中心に、Python標準ライブラリだけで実装しています。

**最初に行うことは、現在のフォルダ構成のままバックアップすることです。既存プロジェクトの移動は不要です。**

## 1. 採用した構成と設計判断

**Python製の収集・検査・復旧ツール + restic + 既存Gitリポジトリ**を採用しました。新規プロジェクトの標準パスは `C:\Dev\Projects` を推奨しますが、バックアップの前提条件にはしていません。GitHub・Azure DevOps等、Gitのホスティング先には依存しません。

| 判断 | 理由・トレードオフ |
|---|---|
| resticへ一本化 | 暗号化、重複排除、圧縮、世代管理、保存先対応、復元は既存エンジンに任せる。Kopiaも有力だが、今回はCLI呼出し・終了コード・自動検証を重視した。二重対応による実装と復旧手順の分岐を避ける |
| ファイル収集をPython側で行う | CodexのSQLiteはOnline Backup APIで取り出し、検査した保存用コピーをresticへ渡す。DB名や世代番号を固定せず、ヘッダで識別する |
| 一時領域へ全量コピー | 世代ごとに保存する範囲を固定し、同じ範囲を検証できる。一時領域と毎回の全量読み書きが必要。保存先ではresticが重複排除するが、一時コピーのI/Oは削減されない |
| hot / coldを区別 | hotは稼働中の取り逃しを減らす用途。coldは全関係アプリを終了して取る復旧基準点。hotでDB単体が正常でも、複数DB・JSONL・コードが同時点とは保証しない |
| まず展開し検証してから切替 | 復元を空フォルダへ限定し、原環境への直接上書きを避ける。明示的なactivate操作では既存フォルダを退避し、切替失敗時は戻す |
| 生のJSONLを保管 | 将来のCodex変更に備えて原本を保持する。Markdownと検索用SQLiteは原本から再生成できる派生物とする |
| 未追跡・Git無視対象も収集 | Gitにない `.env`、作業中ファイル、資料も保護する。`.gitignore`をバックアップ除外として使わない |
| 世代削除を日常バックアップから分離 | バックアップ成功と過去世代の削除を結び付けない。retentionは既定で削除予定表示のみ。pruneも別操作とする |
| 認証情報は既定で除外 | `auth.json`を除外し、復旧後は再ログインする。resticのパスワードはWindows DPAPIで保護し、別途パスワード管理ソフトにも残す |

VSSは今回使っていません。保存用コピーを作った後のファイルだけをresticに渡すため、restic側でそのコピーにVSSを適用しても、Codex全体の同時点整合性は改善しません。ロック中の通常ファイルを読めないときは失敗として終了し、関係アプリを閉じて再実行します。

## 2. できること・対象範囲

- `CODEX_HOME`全体を基本範囲とし、`sessions`、`archived_sessions`、索引、SQLite、設定、Codex管理worktree等を収集します。ファイル名の固定一覧に依存しません。
- `sqlite_home`が外部なら別保存元として登録します。初回検出の優先順は、明示的な `--sqlite-home`、`config.toml`のトップレベル `sqlite_home`、`CODEX_SQLITE_HOME`、`CODEX_HOME`です。
- 登録したプロジェクト配下の `.git`、未コミット、未追跡、Git無視対象を保存します。
- Git worktreeの外部common directoryを検出し、Gitメタデータの保存元へ追加します。外部にある他のworktree作業ファイルは警告し、自動登録しません。
- SHA-256、サイズ、ファイル一覧、空ディレクトリ一覧、SQLite `quick_check` を検査します。
- 破損・不完全なJSONLも原本保存を優先し、解析できない行を警告します。
- 履歴から既知のユーザー・アシスタント発言をMarkdownとSQLiteへ抽出し、日本語部分一致で検索できます。
- 復旧ツール本体とREADMEも各スナップショットの `recovery/tool` に保存します。設定JSON・パスワードはここに含めません。

### 保存しないもの・制約

- `CODEX_HOME/tmp/arg0`、`tmp/arg0-windows`、`tmp/arg0-linux`、`tmp/arg0-macos` はCodexが生成する実行用ヘルパーの一時フォルダとして、Git検出・一覧取得・コピーから除外します。`tmp`全体や`.tmp`全体の除外ではなく、通常の資料や別途登録したプロジェクト内の同名フォルダは保存します。

- Codex保存元では `.codex-provisioning-*.guard`、直下の `thread-writer-locks/*.lock` と `mcp-oauth-locks/*.lock` を実行時ロックとして除外します。Codex保存元と外部SQLite保存元では、配下の `.sqlite-maintenance.lock` も除外します。除外内容をマニフェストに記録し、対象ファイルはSQLite判定のためにも開きません。
- 一般の `*.lock` を一括除外しません。OAuthディレクトリ内の `.enterprise-generation` や `.enterprise-credential-version` 等は保存します。会話JSONL、DB本体、登録プロジェクトのファイルは従来どおり対象です。別の場所で読み取りエラーが起きた場合は失敗として報告します。
- SQLiteの `-wal` / `-shm` / `-journal` は、識別済みDBの補助ファイルとしてSQLite APIで扱い、個別の形式判定や通常コピーはしません。未識別の補助ファイルを一律に無視する変更ではありません。設計根拠は [docs/UPSTREAM_LOCKS.md](docs/UPSTREAM_LOCKS.md) を参照してください。

- 既定のプロジェクト除外は `node_modules`、`__pycache__`、`.venv`、`venv`、`.pytest_cache` です。ディレクトリ名だけでなく、同名のパス要素に適用します。必要な資産がある場合は設定から除外ルールを外してください。
- `auth.json`は既定で除外します。他の設定や会話に秘密情報が含まれる可能性はあります。バックアップ全体を機密として扱ってください。
- シンボリックリンク、ジャンクション、再解析ポイントをたどりません。省略位置を記録・警告します。必要な実体フォルダを `extra_sources` 等へ別途登録してください。
- WSLのLinux側、別OSユーザー、別の `CODEX_HOME`、クラウド上の会話、外部添付、CODEX_HOME外のアプリ固有UI情報、OS資格情報ストアは自動収集しません。
- WindowsのACL・所有者・代替データストリーム、ハードリンク関係、ファイルID、拡張属性を忠実に復元するシステムイメージではありません。通常のファイル内容とディレクトリの保全が目的です。
- Codexの内部DBのスキーマや記録済みパスは書き換えません。復元した会話をCodexアプリが必ず表示・再開できるとは保証しません。
- アーカイブはAI要約をしません。ツール出力・内部推論・添付実体をMarkdownへ完全変換するものでもありません。対応外の構造は原本を参照します。
- SQL Serverなど外部の業務DBは対象外です。独自の正式なバックアップを利用してください。

## 3. 推奨する配置

```text
C:\Dev\Tools\codex-vault\                ツール本体（このZIPを展開）
C:\Dev\Projects\                        今後の新規開発の標準。既存案件は現状の場所でよい
%LOCALAPPDATA%\CodexVault\vault.json     対象・保存先の設定
%LOCALAPPDATA%\CodexVault\repository.dpapi
%LOCALAPPDATA%\CodexVault\work\          保存前の一時コピー・検査結果
%LOCALAPPDATA%\CodexVault\logs\          スケジュール実行ログ
E:\CodexVault\restic\                    外付けSSD等の暗号化保存先
```

保存先をPC本体から独立させてください。同じPCの別パーティションだけではPC故障に備えられません。外付けSSD・NASに加え、別拠点またはクラウドの独立した2つ目のresticリポジトリを推奨します。Gitリモートへのpushも継続します。

一時コピーには会話やソースが平文で存在します。Windowsではユーザー用のアクセス制御を設定し、成功後に削除します。失敗・強制終了時は診断用に残ります。システムドライブのBitLocker等も利用してください。通常削除はSSD上の安全消去を保証しません。

一時領域には**登録した全対象の容量以上**、切替時には復元先にも新しいコピーを置く余裕が必要です。大量のビルド成果物等を含む場合は、除外を確認し、定期実行を120分等に延ばしてください。

## 4. 初回セットアップ（Windows）

### 4.1 必要なもの

- Python **3.11以上**。3.12等の公式配布版を推奨。追加のPythonパッケージは不要です。
- restic **0.17以上**。この版は **restic 0.19.1 / Python 3.12**で実物の暗号化保存・復元を検証しました。
- Git。履歴・worktreeの診断に利用します。Git自体のインストールは行いません。
- Windows PowerShell 5.1以上。定期実行の登録用です。

resticは [公式インストール案内](https://restic.readthedocs.io/en/stable/020_installation.html) に従って導入してください。Windowsの公式リリースZIPから取り出した実行ファイルを `restic.exe` としてPATHへ配置する方法が使えます。`winget search restic` で表示されるパッケージから導入する場合も、提供元と内容を確認してください。このツールが外部プログラムを自動インストールすることはありません。

```powershell
python --version
restic version
git --version
Set-Location C:\Dev\Tools\codex-vault
python -m codex_vault --help
```

`python`が見つからない場合は `py -3.12` 等で実行します。以後の例では `python` と記載します。スケジュールではランチャーでなく実際の `python.exe` の絶対パスを使います。

### 4.2 設定を作る

```powershell
python -m codex_vault configure `
  --repository 'E:\CodexVault\restic' `
  --project 'C:\Dev\Projects' `
  --project 'D:\現在作業している案件'
```

上記パスは実在するフォルダに置き換えます。存在しないフォルダを登録すると失敗します。`C:\Dev\Projects`全体と、その中の個別案件を重複登録しないでください。

引数なしの `configure` は保存先とプロジェクトパスを質問します。履歴だけを先に保全するときは `--history-only` を指定します。

```powershell
python -m codex_vault configure --repository 'E:\CodexVault\restic' --history-only
```

別の保存場所を使っている場合は `--codex-home` と `--sqlite-home` を明示できます。環境変数は初回に固定した絶対パスへ変換します。定期実行時の環境差で保存元が変わることを防ぐためです。`config.toml`のプロファイル、起動時の `-c`、管理ポリシー等で保存先が変わる場合は実際のパスを明示してください。

`--sqlite-home`を明示した設定では `sqlite_home_explicit: true` を記録します。doctorは自動検出値との差を注意事項として表示します。手作業で明示パスを登録するときも、この値を適切に設定してください。

`%LOCALAPPDATA%\CodexVault\vault.json` が作成されます。対象追加はこのJSONの `projects` / `extra_sources` を編集して行います。例は同梱の `config.example.json` を参照してください。パスはJSON内では `C:/Dev/Projects` と書くと簡単です。

既存設定への `configure` の再実行は拒否します。変更時は既存JSONを編集してください。`vault_id` は作り直さず保持します。

### 4.3 保存用パスワードを登録する

```powershell
python -m codex_vault credential
```

12文字以上の強いパスワードを2回入力します。画面には表示しません。Windowsの現在ユーザー用DPAPIで保護します。

**同じパスワードを、PCと独立したパスワード管理ソフト等にも必ず記録してください。DPAPIファイルだけでは別PCへ復旧できません。** Windowsアカウント名が同じでも、新しいPCでそのまま復号できるとは限りません。

### 4.4 リポジトリを初期化し、対象を確認する

```powershell
python -m codex_vault init
python -m codex_vault doctor
python -m codex_vault scan
```

`init`は最初の1回だけです。既存リポジトリに対して繰り返す必要はありません。`doctor`は保存元、検出したSQLite保存先、保存先への接続、空き容量を表示します。`scan`は過去の会話から作業パス候補を読み取り専用で抽出します。候補を勝手に登録・移動しません。

`covered: false`の重要案件を `projects` へ追加してください。既に消えたフォルダ、古いworktree、別OSのパスは自動復旧できません。ファイル内容がプロジェクトルート外にあるものは `extra_sources` に追加します。

設定・作業領域・resticリポジトリを保存元の中へ置く構成は、自己参照を防ぐため拒否します。

### 4.5 初回はcoldで保存する

Codexアプリ、Codex CLI、IDE拡張、対象を編集・ビルド・同期している処理を終了します。その後、通常のPowerShellから実行します。

```powershell
Set-Location C:\Dev\Tools\codex-vault
python -m codex_vault backup --mode cold --stopped
python -m codex_vault snapshots
python -m codex_vault check --read-data
```

`--stopped`は実行者の停止確認です。すべてのプロセスが停止したことを自動的に証明するフラグではありません。cold中に保存元のファイル一覧・サイズ・更新時刻が変わると失敗させます。それでもプロジェクトのアプリ上の未保存内容は保存できません。

**初回バックアップの後は、下の復元テストを必ず1回実施してから定期運用へ移行してください。**

## 5. 日常の運用手順

| タイミング | 操作 | 確認すること |
|---|---|---|
| ログオン中30分ごと | hotバックアップを定期実行 | 終了コード、警告、最後の成功時刻 |
| 1日の作業終了後・更新前 | 関係アプリを終了してcold保存 | 終了コード0。コード2なら省略・警告を確認 |
| 毎週 | `check` | リポジトリ構造の検査。全データ読出しではない |
| 毎月 | `check --read-data` | 全保存データの読出し・整合性検査 |
| 毎月 | `retention`で予定確認後、必要なら適用 | 重要な過去世代が残ること |
| 四半期ごと・PC変更前 | 空フォルダへの復元テスト | ハッシュ・DB検査と、重要な履歴・ソースを実際に開く |
| 日常 | Git commit / push | リモート側でも開発履歴を保持 |

### 手動コマンド

```powershell
python -m codex_vault backup --mode hot
python -m codex_vault status
python -m codex_vault check
```

`status`には最後の成功と最後の失敗を別々に残します。最後の失敗は成功後も残るため、両者の時刻を比較します。`hours_since_success`が24時間以上なら、保存先の接続とタスクの実行状況を確認してください。異常をメール等へ送る通知機能は含みません。

### タスクスケジューラへ登録

```powershell
$configPath = Join-Path $env:LOCALAPPDATA 'CodexVault\vault.json'
$pythonExe = (& python -c 'import sys; print(sys.executable)').Trim()
.\scripts\Install-ScheduledTasks.ps1 -Config $configPath -PythonExe $pythonExe
```

毎30分のhotバックアップと、日曜09:00の `check` を登録します。時刻はWindowsのローカルタイムです。日本のPCなら日本時間で動きます。週末もバックアップします。

**現在ユーザーのログオン中のみ実行します。** ロック中もログオンが継続していれば対象です。ログオフ・電源OFF中の取得はできません。Windowsパスワードをタスクに保存しません。cold保存はアプリの停止が必要なため自動登録しません。チェックとの重複はツールのロックで拒否されるため、次回または手動で再実行します。

大量のプロジェクトを保存する場合の例:

```powershell
.\scripts\Install-ScheduledTasks.ps1 -Config $configPath -PythonExe $pythonExe `
  -NamePrefix 'CodexVaultSlow' -BackupIntervalMinutes 120
```

タスク名が既に存在する場合は上書きしません。不要な旧タスクをタスクスケジューラで削除してから再登録します。登録・実行が会社のポリシーで制限される場合は、管理者へ相談するか手動運用します。PowerShell実行ポリシーの恒久的な緩和は不要です。社内規則に従ってファイルのブロック解除や署名を行ってください。

実行ログは設定ファイルと同じフォルダの `logs` に30日間保持します。タスクスケジューラの「前回の実行結果」も確認してください。

| 終了コード | 意味 |
|---|---|
| `0` | 成功。hotの一般的な同時点整合性の注意だけでは警告扱いにしない |
| `2` | 保存は完了したが、リンク省略・JSONL解析不可・保存中の更新等の個別警告がある |
| `1` | 失敗。resticの部分成功コード3もツールでは失敗扱い。前回の成功世代を保持する |

予期しない強制終了等ではPythonやOSの別の終了コードになる場合もあります。0以外はログを確認してください。

### 一時コピーの削除と再実行

前回の取得失敗後に一時コピーが残った場合、次回の取得開始時に削除します。WindowsでGitオブジェクト等の読み取り専用属性がコピー先へ引き継がれていても、`work_dir/payload`内だけで属性を解除し、削除を一度再試行します。元の`.codex`、プロジェクト、保存済みリポジトリや復元前の退避物には適用しません。ACL変更や使用中ロックの強制解除は行わず、それらの問題が残る場合は失敗を報告します。

片付けだけを明示的に行うコマンド:

```powershell
python -m codex_vault --debug clean-stage
```

暗号化スナップショット作成後に片付けだけが失敗した場合は、保存済みIDと警告を返し、`stage_cleanup_complete: false`を記録します。この場合は終了コード2です。未暗号化の一時コピーが残るため、原因を確認して`clean-stage`を再実行します。バックアップ開始前に古い一時コピーを削除できなかった場合は、バックアップ失敗として終了します。

### 保存期間と容量整理

```powershell
python -m codex_vault retention
```

既定は削除予定の表示だけです。直近2日以内の全世代、日次30、週次8、月次12を保持する方針です。hot/coldとホスト・保存パス別に保持するため、頻繁なhot保存がcold基準点を押し出しません。実行されなかった日の世代を新たに作るものではありません。

予定を確認し、削除してよい場合だけ実行します。

```powershell
python -m codex_vault retention --apply
python -m codex_vault prune
```

`retention --apply`は該当する古いスナップショットを削除します。`prune`はリポジトリ全体の未参照データを回収し、続けて `check` します。時間がかかり、その間バックアップができないことがあるため、手動で余裕のある時間に実行してください。他のvaultの世代はretentionの対象にしません。

長期保存したい節目がある場合は、まず別の独立したリポジトリへ保全して、その保存方針を別管理してください。この版に「永久保護」タグを付ける機能はありません。

## 6. 復元テストと復旧手順

### 6.1 空フォルダへ展開する（原環境はそのまま）

```powershell
python -m codex_vault snapshots
python -m codex_vault restore --snapshot latest --target 'C:\Dev\Recovery\test-20261028'
```

対象はこの設定の `vault_id` に属する世代に限定します。任意のIDを選ぶときは、一覧にある8桁以上のスナップショットIDを指定します。

```powershell
python -m codex_vault restore --snapshot '12345678' --target 'C:\Dev\Recovery\chosen'
```

展開先にファイルが存在する場合は拒否します。保存元・一時領域・リポジトリと重なる場所も拒否します。復元中に失敗したフォルダは残るため、次回は別の空フォルダを使ってください。

成功時に `payload`の場所、保存元IDと元パス、取得モード、警告を表示します。展開後に自動検証しますが、再検査もできます。

```powershell
python -m codex_vault verify --payload 'C:\Dev\Recovery\test-20261028\payload'
```

```text
payload/
  manifest.json            保存元と元パス・ハッシュ・除外・警告・Git情報
  sources/
    codex/                 CODEX_HOMEの保存内容
    sqlite/                外部SQLite保存先（ある場合）
    project-001/           登録したプロジェクト
    extra-001/             明示追加した外部フォルダ（ある場合）
    git-.../               外部Gitメタデータ（ある場合）
  recovery/tool/           このツールとREADME
```

`sources`のIDと元パスは毎回のマニフェストで確認してください。設定の登録順を変えるとprojectの番号も変わります。番号だけから案件名を推測しないでください。

ハッシュ照合は保存時点のコピーと復元物の一致確認です。元の会話が論理的に完全であること、Codexで再開できること、ソースがビルドできることまで証明しません。重要なファイルを開き、必要な環境でビルドも確認してください。

### 6.2 Codex上での復旧を試す

1. Codex・CLI・IDE等を終了し、復元先を編集する処理を止めます。
2. 可能なら問題が起きた現在の状態も、別世代または別ディスクへ保全します。
3. 同じパス・同じOSユーザー・近いCodexバージョンでの復旧から試します。
4. `verify`出力で元パスと保存元IDを照合します。
5. 以下のactivateで1保存元ずつ切り替えます。外部SQLiteを含む必要な全保存元の切替が終わるまでCodexを起動しません。

```powershell
$payload = 'C:\Dev\Recovery\chosen\payload'
python -m codex_vault activate --payload $payload --source codex `
  --target "$env:USERPROFILE\.codex" --stopped --apply
```

上記は既定CODEX_HOMEの場合です。独自パスなら元の設定へ置き換えます。外部SQLiteがある場合の例:

```powershell
python -m codex_vault activate --payload $payload --source sqlite `
  --target 'D:\CodexState' --stopped --apply
```

プロジェクトも戻す必要がある場合:

```powershell
python -m codex_vault activate --payload $payload --source project-001 `
  --target 'C:\Dev\Projects' --stopped --apply
```

`project-001`がフォルダ全体を指す例です。個別案件を登録している場合は、その案件の元パスを指定します。**対象フォルダ全体が切り替わる操作なので、元パスとIDを必ず照合してください。**

activateはコピー内容を検査後、既存フォルダを `元名.before-vault-日時...` へリネームして、新しいコピーを配置します。切替失敗時は元フォルダを戻します。退避先は自動削除しません。処理は1保存元単位で、複数保存元を一括トランザクションとして戻すものではありません。すべての必要な保存元が戻ったことを確認してから起動してください。

停止の確認ができない場合や、復旧を試すだけの場合はactivateせず展開物を参照します。`CODEX_HOME`外の認証・UI情報は再設定が必要な場合があります。`auth.json`を除外した場合は再ログインしてください。

### 6.3 切替を取り消す

Codex等を再び終了します。activateが返した `previous` の正確なパスを使い、復旧後フォルダを別名へ退避してから元に戻します。例:

```powershell
# 実際にactivateが表示したパスへ置き換えてから実行
$target = Join-Path $env:USERPROFILE '.codex'
$previous = 'C:\Users\USER\.codex.before-vault-実際の日時'
$failed = $target + '.recovery-attempt-' + (Get-Date -Format 'yyyyMMdd-HHmmss')
if (-not (Test-Path -LiteralPath $previous -PathType Container)) { throw '退避先がありません' }
Move-Item -LiteralPath $target -Destination $failed -ErrorAction Stop
Move-Item -LiteralPath $previous -Destination $target -ErrorAction Stop
```

外部SQLite等も切り替えた場合は、同じバックアップ時点へ揃えて戻します。退避フォルダを削除するのは、履歴・コード・ビルドを確認してからです。

### 6.4 会話を読み物・検索データとして救出する

```powershell
python -m codex_vault archive `
  --payload 'C:\Dev\Recovery\chosen\payload' `
  --output 'C:\Dev\Recovery\archive-20261028'
python -m codex_vault search `
  --archive 'C:\Dev\Recovery\archive-20261028' --query 'RabbitMQ'
```

出力先は空フォルダにします。`conversations/*.md`、`index.sqlite`、`archive-report.json`を生成します。会話ID、元パス、原本SHA-256を保持します。同じ発言が二重記録される既知の形式ではresponse_itemを優先し、event_msgとの重複を抑えます。検索は最大50件の部分一致です。

この出力は平文です。暗号化ドライブ等で管理してください。未対応形式・解析不可行の件数をreportで確認します。原本はpayloadのJSONLに残っています。

### 6.5 PCが故障し、設定もツールも失った場合

1. 新PCにresticを導入し、外部バックアップ先を接続します。
2. 別途保管した**元のresticパスワード**を用意します。DPAPIファイルだけに頼れません。
3. restic単体で一覧・展開します。パスワードは対話入力します。

```powershell
restic -r 'E:\CodexVault\restic' snapshots
restic -r 'E:\CodexVault\restic' restore '実際のスナップショットID' `
  --target 'C:\Dev\Recovery\disaster'
```

`latest`より、一覧で確認した取得日時とIDを明示することを推奨します。部分成功として残った世代は検証に失敗する場合があります。そのときは以前のcold世代から試します。

4. 復元した `payload\recovery\tool` からPythonツールを利用できます。

```powershell
Set-Location 'C:\Dev\Recovery\disaster\payload\recovery\tool'
python -m codex_vault verify --payload 'C:\Dev\Recovery\disaster\payload'
```

5. `manifest.json`で元パス・除外・警告を確認します。まずファイルの救出とアーカイブを行えます。
6. 新しい設定を作成する場合、復元したmanifestの `vault_id` を設定へ引き継ぐと、ツール経由でも以前の世代を参照できます。保存元パスは新PCの環境に合わせます。
7. Windowsで `credential` に**既存リポジトリと同じパスワード**を登録します。既存リポジトリには `init` を再実行しません。
8. 同じパスへ復旧し、設定ができたら上記activateを使えます。パスを変える場合は、新規セッションとアーカイブからの引継ぎを基本とし、既存DBを一括置換しません。

### Git worktreeがある場合

`.git`がファイルであるリンクされたworktreeには元の絶対パス等が記録されています。Gitメタデータも保存しますが、別PC・別パスでは `git worktree repair` 等による修復が必要です。`manifest.json`の `git`、`warnings`、`sources`を確認してください。

未登録worktreeの未コミット・未追跡ファイルは、Git common directoryの保全だけでは救えません。警告された重要なworktreeは、運用開始時に `projects`へ追加します。サブモジュール、Git LFS、外部オブジェクト参照、外部の依存データは、依存先も保全し、実際にチェックアウト・ビルドできることを復旧テストで確認してください。

## 7. 独立した2つ目のバックアップ先

単一の外付けディスクだけに依存しないための手順です。稼働中のresticリポジトリを通常の同期ソフトでコピーするのではなく、別設定から2つ目のリポジトリへ独立して保存します。二度収集するためI/Oは増えます。

例として、別の外付けドライブへ設定を作ります。

```powershell
$primaryPath = Join-Path $env:LOCALAPPDATA 'CodexVault\vault.json'
$secondaryRoot = Join-Path $env:LOCALAPPDATA 'CodexVaultSecondary'
New-Item -ItemType Directory -Path $secondaryRoot -Force | Out-Null
$secondaryPath = Join-Path $secondaryRoot 'vault.json'
if (Test-Path -LiteralPath $secondaryPath) { throw '既に設定があります' }
$settings = Get-Content -LiteralPath $primaryPath -Raw | ConvertFrom-Json
$settings.repository = 'F:\CodexVault\restic'
$settings.work_dir = Join-Path $secondaryRoot 'work'
# vault_idとcredential_fileは元設定を保持。DPAPIは現在ユーザー用。
$settings | ConvertTo-Json -Depth 20 | Set-Content -LiteralPath $secondaryPath -Encoding UTF8
python -m codex_vault --config $secondaryPath init
python -m codex_vault --config $secondaryPath backup --mode hot
```

以後、2つ目の設定でも保存・検査・復元テストします。定期登録する場合は `-NamePrefix CodexVaultSecondary` 等で名前を分けます。リポジトリは独立しており、既存世代を複製する操作ではありません。2つ目を開始する以前の世代は入りません。

NASならUNCパスを推奨します。ユーザーのログオンセッションからアクセスできることを確認してください。クラウドならrestic公式の保存先形式（SFTP、S3等）へ設定を変え、必要な認証はrestic公式手順で設定します。クラウド資格情報・ネットワーク設定・削除防止の構成はこのツールでは自動設定しません。

暗号化だけではランサムウェアや誤操作によるバックアップの削除を防げません。片方を取り外す、別アカウントへ分離する、保存先の削除防止を利用する等、独立性を持たせてください。

## 8. エラー時の対応

| 状況 | 対応 |
|---|---|
| SQLite取得・通常ファイルコピーが失敗 | Codex・IDE等を終了してcoldで再実行。元データは削除しない |
| JSONL解析不可の警告 | 保存された原本と過去世代を保持。`archive-report.json`で解析位置を確認 |
| リンク・ジャンクションを省略 | 指している実体を別保存元として追加。原フォルダを自動移動しない |
| 保存先ドライブが切断 | 接続を戻し、`doctor`後に再実行。バックアップ操作でリポジトリを勝手に新規作成しない |
| パスワード不一致・DPAPI失敗 | 設定とWindowsユーザーを確認。別PCなら保管した元パスワードを登録。既存repoを消したり初期化し直さない |
| ロックが使用中 | 別処理の終了を待つ。ツール自身のロックはプロセス終了時にOSが解放する。resticのロックは公式手順で調査し、稼働処理がないと確認してから対処 |
| 古い一時コピーが残っている | 原因確認後、下の `clean-stage` で削除。次の取得開始時にも置き換える |
| 復元検証に失敗 | その展開物を原環境に適用しない。別の空フォルダへ以前の世代を復元し、保存先の `check --read-data` を行う |
| `doctor`でSQLite保存先が異なる | 実際のアプリの保存先を確認し、設定を更新。移動前の重要なデータも別保存元で保全 |

```powershell
python -m codex_vault clean-stage
```

ログにはパスやエラー情報が含まれます。共有前に機密情報を確認してください。ツールがresticパスワードを画面・設定JSON・ログに意図的に出力することはありません。

## 9. Linux / macOS / WSL

Pythonの基本バックアップ・検証・アーカイブ処理は共通です。DPAPIとPowerShellの定期登録はWindows専用です。WindowsとWSLは**別々の設定・保存元**として扱い、Windows側だけの取得でWSLも保全済みと判断しないでください。

Windows以外ではrestic用のパスワードファイルをツール外の安全な場所に作り、所有者のみ読める権限にします。コマンド履歴へパスワードを直書きしないでください。

```bash
export RESTIC_PASSWORD_FILE=/absolute/private/restic-password.txt
python3 -m codex_vault --config /absolute/private/vault.json configure \
  --repository /mnt/backup/codex-restic --project /home/user/projects
python3 -m codex_vault --config /absolute/private/vault.json init
```

macOSのキーチェーンやLinuxのシークレットストアからCodex認証を取り出す機能はありません。再ログインします。Windows固有のジャンクション・ACL・タスクスケジューラ・DPAPIについてはWindows実機で初回検証を行ってください。

## 10. 検証・保守

```powershell
python -m unittest discover -s tests -v
```

resticがPATHにあれば実物を使った暗号化保存・復元テストも実行します。任意の実行ファイルを指定する例:

```powershell
$env:VAULT_TEST_RESTIC = 'C:\Tools\restic.exe'
python -m unittest discover -s tests -v
```

テストは一時ディレクトリの合成データを使い、実際のユーザーのCODEX_HOMEやプロジェクトを対象にしません。検証の詳細と環境上の制約は [docs/VALIDATION.md](docs/VALIDATION.md) に記載します。

Codex更新に伴う内部ファイル名の変化には全体収集で対応し、JSONL形式の変化には原本保持で備えます。アップデート直前にcold保存し、更新後は履歴の表示とバックアップを確認してください。

本ツールはOpenAI公式のバックアップ製品ではありません。元データを保持する仕組みと、Codexアプリによる履歴認識は別です。アプリへの完全復旧を保証するものではなく、原本・開発ファイル・閲覧可能な履歴の保全を優先しています。

## 11. 参照した公式資料

設計確認日: **2026-10-28**。実装は内部DBのテーブル構造に依存しません。

- [OpenAI: Codex環境変数と保存先](https://developers.openai.com/codex/config-file/environment-variables)
- [OpenAI: 設定リファレンス](https://developers.openai.com/codex/config-reference/)
- [SQLite Online Backup API](https://www.sqlite.org/backup.html)
- [restic: バックアップと終了コード](https://restic.readthedocs.io/en/stable/040_backup.html)
- [restic: 復元](https://restic.readthedocs.io/en/stable/050_restore.html)
- [restic: 世代削除とprune](https://restic.readthedocs.io/en/stable/060_forget.html)
- [Kopia: 機能一覧](https://kopia.io/docs/features/)

ライセンスはMITです。同梱コードは改修・社内利用できます。restic自体の配布物は同梱していません。

## エラーの詳細診断

```powershell
python -m codex_vault --debug backup --mode hot
```

トレースにはパスや例外メッセージが含まれます。共有前に個人名・社内パス・秘密情報を伏せてください。変数一覧・設定全体・環境変数は出力しません。hotの権限エラーを一律無視する機能ではありません。
