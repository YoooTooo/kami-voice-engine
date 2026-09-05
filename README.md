# Kami Voice Engine

音声ファイルをRVCでアマテラスの声へ変換する、プロジェクト非依存の音声変換ツールです。

- Mac上の既存ApplioをUIなしで呼び出す
- 複数ファイルを1回のモデル読み込みでまとめて変換する
- Macから1コマンドでGitHub ActionsのCPU変換を依頼する
- モデル・入力・結果を非公開Cloudflare R2で受け渡す

MVPの責務は **音声ファイル入力 → 音声ファイル出力** だけです。TTS生成、クイズJSON、動画レイアウト、投稿処理には依存しません。

## 現在の状態

次の経路で、実際にアマテラス音声へ変換できることを確認済みです。

1. MacのApplio CLIによる単発変換
2. MacのApplio CLIによる7ファイルのバッチ変換
3. GitHub ActionsのLinux CPUによる単発変換
4. GitHub ActionsのLinux CPUによる7ファイルのバッチ変換
5. Macからの1コマンド操作による、アップロード・Actions起動・待機・ダウンロード
6. R2の`jobs/`配下を90日後に自動削除するLifecycle rule

GitHub Actionsの汎用バッチは手動起動です。定期実行やcontent-engineとの自動接続はまだ追加していません。

## 構成

```text
kami-voice-engine/
├── .github/
│   └── workflows/
│       └── cpu-smoke-test.yml   # Linux CPUの汎用R2バッチ変換
├── .gitignore
├── kami_voice.py                # ローカルApplio変換アダプター
├── kami_remote.py               # Macからクラウド変換を依頼するCLI
└── README.md
```

Applio本体とRVCモデルは、この公開リポジトリには含めません。

## 変換設定

現在のデフォルトは、Kokoro TTSのdefault American femaleからアマテラスへ変換して良好だった設定です。

| 項目 | 値 |
| --- | ---: |
| Pitch | `2` |
| Index rate | `0.84` |
| Volume envelope | `1` |
| Protect | `0.5` |
| F0 method | `rmvpe` |
| Embedder | `contentvec` |
| Speaker ID | `0` |
| Export format | `WAV` |
| Formant shifting | 無効 |

自分の声を入力する場合はPitch `11`とApplioの`mtof`フォルマントプリセットを使用していますが、フォルマントの数値はまだCLI設定として確定していません。

## 固定しているApplio

動作確認したApplioのGitコミット：

```text
085197e738ce9dd4c0bae1e0a74df5de25b89444
```

Macで確認した環境：

```text
Applio: 3.6.4-7-g085197e7-dirty
Python: 3.12.14
```

GitHub Actionsも同じコミットとPython 3.12.14を使用し、CPU版PyTorchで動作させます。

## 必要なもの

### ローカル変換

- macOSまたは互換性を確認したLinux
- 既存のApplio
- Python
- FFmpeg / ffprobe
- アマテラスの`.pth`と`.index`

デフォルトでは次のMac配置を使用します。

```text
~/Applio/
~/Applio/.venv/bin/python
~/Applio/logs/amaterasu/amaterasu.pth
~/Applio/logs/amaterasu/amaterasu.index
```

### クラウド変換

- `aws` CLI
- `gh` CLI
- `gh auth login`済みのGitHubアカウント
- Cloudflare R2のS3互換キーを登録したAWS CLIプロファイル
- GitHub ActionsのRepository secrets

macOSでのCLI導入例：

```bash
brew install awscli gh
gh auth login
```

## ローカルで変換する

### 1ファイル

リポジトリ直下で実行します。

```bash
cd "$HOME/kami-voice-engine"

python3 kami_voice.py convert input.wav \
  --output output/answer-rvc.wav
```

既存出力は上書きしません。意図的に置き換える場合だけ`--overwrite`を付けます。

```bash
python3 kami_voice.py convert input.wav \
  --output output/answer-rvc.wav \
  --overwrite
```

### フォルダを一括変換

入力フォルダ直下のWAV、MP3、FLAC、OGG、M4Aを対象にします。出力先には存在しない新しいフォルダを指定します。

```bash
cd "$HOME/kami-voice-engine"

python3 kami_voice.py batch input/week \
  --output output/week \
  --expected-count 7
```

出力：

```text
output/week/
├── <入力ファイル名>.wav
└── batch-result.json
```

`batch-result.json`にはファイルごとの成功・失敗、音声情報、設定、処理時間が記録されます。Applioが途中で失敗しても、完全に検証できた出力だけを残します。

### 別のApplioやモデルを指定する

```bash
python3 kami_voice.py convert input.wav \
  --output output/answer-rvc.wav \
  --applio-dir /path/to/Applio \
  --python /path/to/Applio/.venv/bin/python \
  --model /path/to/model.pth \
  --index /path/to/model.index \
  --pitch 2
```

## Macからクラウド変換する

`kami_remote.py`は次の処理を自動で行います。

```text
入力を非公開R2へ送信
→ GitHub Actionsを起動
→ 完了まで待機
→ 変換済みWAVを非公開R2からMacへ取得
```

### 1ファイル

```bash
cd "$HOME/kami-voice-engine"

python3 kami_remote.py input.wav \
  --output output/cloud-result
```

入力が`input.wav`なら、結果は次の場所に保存されます。

```text
output/cloud-result/input.wav
```

### フォルダを一括変換

```bash
python3 kami_remote.py input/week \
  --output output/cloud-week
```

`--request-id`を省略すると、衝突しないIDを自動生成します。指定する場合は英数字、ピリオド、アンダースコア、ハイフンを使用できます。

```bash
python3 kami_remote.py input/week \
  --output output/cloud-week \
  --request-id daily-2026-09-05 \
  --pitch 2
```

安全のため、次の場合は処理を開始しません。

- ローカル出力フォルダが既に存在する
- 同じrequest IDのR2入力が既に存在する
- 入力フォルダが空
- 同じstemを持つ入力が複数ある
- 1回の依頼が100ファイルを超える

待機中にCLIを中断しても、GitHub Actions側のジョブは継続する場合があります。GitHub Actions画面で状態を確認してください。

## Cloudflare R2

使用バケット：

```text
kami-voice-private
```

Public Accessは無効にします。

### オブジェクト構成

```text
kami-voice-private/
├── amaterasu/
│   ├── amaterasu.pth
│   └── amaterasu.index
└── jobs/
    └── <request-id>/
        ├── input/
        │   └── <original-name>.<audio-extension>
        └── output/
            └── <github-run-id>-<attempt>/
                ├── audio/
                │   ├── <original-stem>.wav
                │   └── batch-result.json
                ├── inference-time.txt
                └── packages.txt
```

`amaterasu/`は永続保存対象です。`jobs/`は入力と変換結果の一時保管領域で、Prefix `jobs/`を対象に90日後に削除するLifecycle ruleを設定済みです。削除期限はモデルの`amaterasu/`には適用されません。

### Mac用AWS CLIプロファイル

プロファイル名：

```text
kami-voice-upload
```

設定：

```bash
aws configure --profile kami-voice-upload
```

入力値：

```text
AWS Access Key ID: Cloudflare R2のAccess Key ID
AWS Secret Access Key: 対応するSecret Access Key
Default region name: auto
Default output format: json
```

AWS CLIをS3互換クライアントとして利用しています。保存先はAWS S3ではなくCloudflare R2です。

### GitHub Actions Secrets

GitHubの`Settings → Secrets and variables → Actions`に登録します。

| Secret名 | 内容 |
| --- | --- |
| `R2_ACCESS_KEY_ID` | `kami-voice-private`にアクセスできるR2 Access Key ID |
| `R2_SECRET_ACCESS_KEY` | 対応するSecret Access Key |
| `R2_ENDPOINT` | R2のS3 API endpoint |

秘密値をリポジトリ、Issue、ログ、チャットへ貼らないでください。現在のワークフローは結果もR2へ書き込むため、対象バケットだけに限定した`Object Read & Write`権限を使用します。

## GitHub Actionsを画面から起動する

GitHubで次を開きます。

```text
Actions → RVC CPU batch conversion → Run workflow
```

| 入力 | 内容 |
| --- | --- |
| `request_id` | R2の`jobs/<request-id>/input/`に対応するID |
| `expected_count` | 想定ファイル数。`0`なら1本以上を許可 |
| `pitch` | `-24`から`24`。通常は`2` |

同じrequest IDのActionsは並行実行しません。ジョブは最大45分、RVC変換処理は最大18分で停止します。

## 実測結果

約2秒の同一テスト音声を使用した、キャッシュなしの参考値です。音声尺、依存配布状況、GitHubランナーの負荷によって変動します。

| 実行 | ジョブ全体 | 変換ステップ |
| --- | ---: | ---: |
| 1ファイル | 3分24秒 | 31秒 |
| 7ファイル・1バッチ | 4分14秒 | 1分8秒 |

7ファイルを個別のジョブにせず、1回のバッチにすることで依存インストール・モデル取得・モデル読み込みを共有できます。

Macでの7ファイルバッチ参考値：

```text
バッチ処理: 15.07秒
起動を含む全体: 19.96秒
最大常駐メモリ: 約3.1GB
```

## 失敗時の扱い

`kami_voice.py`の終了コード：

| コード | 意味 |
| ---: | --- |
| `0` | 全ファイル成功 |
| `1` | 変換失敗または一部失敗 |
| `2` | 引数・入力条件のエラー |

次の安全策があります。

- 出力WAVをffprobeと完全デコードで検証する
- バッチ中に一部失敗しても、検証済みファイルは保持する
- 既存出力をデフォルトで上書きしない
- モデルディレクトリをR2の結果へアップロードしない
- GitHub Actionsの成果物へモデルや音声を公開しない
- R2上のrun IDとattemptで実行結果を分離する

利用側では、RVC変換が失敗・未完了の場合に元のTTSを使用するフォールバックを推奨します。

## content-engineとの予定インターフェース

予定している流れ：

```text
nihongo-content-engine
  1. 1週間分のanswer.readingをTTS生成
  2. quiz IDをファイル名にしてR2へアップロード
  3. kami-voice-engineのworkflow_dispatchを起動
  4. 完了したWAVを取得
  5. answer-rvc.wavとして配置
  6. RVCがなければanswer-tts.mp3へフォールバック
  7. Answer動画を生成
```

音声は物理的に連結・再分割せず、個別ファイルを1回のApplioバッチへ渡します。これにより境界のずれを避けながら、モデル読み込みを共有できます。

## 運用上の注意

- RVCは声質変換です。元TTSの発音、アクセント、リズム、抑揚は出力へ影響します。
- モデルの利用条件は、モデル提供元のライセンス・規約を別途確認してください。
- `.pth`と`.index`を公開リポジトリへコミットしないでください。
- `output/`や一時音声をGitへ追加しないでください。
- GitHub Actionsの料金、無料枠、利用規約、ランナー仕様は運用開始時にも公式情報を確認してください。
- 現在のActionsはCPU互換性と手動バッチ処理を検証する段階であり、常駐APIやリアルタイム変換ではありません。

## 次の作業

- content-engineからのworkflow起動・完了待機・結果取得
- 依存関係とモデル取得のキャッシュ効果測定
- 実際の7日分TTSによる音質・所要時間確認
- 自声用フォルマント設定の数値化
