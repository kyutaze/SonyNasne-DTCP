# DTCP-IP 録画救出ツール (dtcpip-rescue)

公式 FW の nasne に残っている録画を、DTCP-IP の正規の経路 (AKE + ストリーム受信)
で PC 側へ救出するツール。転送はコピーを消費しない方式で行う。
エンジン (AKE と復号) は [pydtcpip](https://github.com/tsukumijima/pydtcpip) が担い、
このツールは列挙・転送ドライバ・保存・検証と、Tkinter 製の簡易 GUI を提供する。

DR 録画・3倍録画のどちらも、この経路で復号済み TS として取得できる。
取得した TS はサイズ・MPEG-TTS の同期バイト・SHA-256 で検証され、
manifest に記録される。

このツールが成立したのは pydtcpip のおかげです。作者の tsukumijima 氏に感謝します。

詳しい仕組みは [docs/50](../../docs/50_dtcpip_rescue_tool_internals.md)、
解析と検証の記録は [docs/48](../../docs/48_dtcpip_rescue_pipeline.md) と
[docs/49](../../docs/49_dtcpip_rescue_tool_and_windows.md) を参照。

## 鍵について (重要)

- **鍵は利用者本人が `credentials.env` にだけ配置する。** このファイル以外に
  鍵を置く場所はない。チャットや他のファイル、コマンドラインに値を出さない。
- このツールのプログラム (CLI / GUI とも) は、鍵の値を **読まない・出さない・
  検証しない**。資格情報は pydtcpip 自身が env ファイルから読み込む。
- ツールに鍵は同梱されない。リポジトリに入っているのは空テンプレート
  (`credentials.env.example`) だけ。

この分担を成り立たせている仕組みは docs/50 §2 を参照。

## 構成

```
tools/dtcpip-rescue/
  README.md              このファイル
  rescue.py              ドライバ (CLI: list / fetch / fetch-all)
  list_recordings.py     列挙 (資格情報なしで録画一覧を JSON で出す)
  rescue_gui.py          GUI (Tkinter、標準ライブラリのみ)
  ts_fix.py              救出した TS を番組情報の読める一般的な TS に変換
  credentials.env.example 資格情報の空テンプレート
  data/                  (実行時に作成) credentials.env の置き場
  out/                   (実行時に作成) 救出した TS と manifest
  logs/                  (実行時に作成) 実行ログ
```

`data/` `out/` `logs/` は `.gitignore` 済みで、コミットされない。
実行時のデータレイアウトの詳細は docs/50 §7 を参照。

## 前提と pydtcpip の用意

- このツール自体は Python 3.10+ (標準ライブラリのみ)。
- pydtcpip は Python 3.13+ が必要。同梱しないので、別途用意して場所を教える。

pydtcpip の用意 (clone + venv の形が、このツールの想定レイアウト):

```
git clone https://github.com/tsukumijima/pydtcpip.git
cd pydtcpip
uv sync            # .venv ができる (uv が無い場合は venv + pip でも可)
```

場所の教え方は、引数 `--pydtcpip-home` または環境変数 `PYDTCPIP_HOME` で
「`.venv` を含む pydtcpip のチェックアウト」を指す。他のパス類も引数 /
環境変数 `RESCUE_*` で変更できる (解決順序の詳細は docs/50 §5)。

資格情報ファイルは、空テンプレート `credentials.env.example` を
`data/credentials.env` にコピーし、利用者本人が値を書く
(変数は `PYDTCPIP_CERTIFICATE` と `PYDTCPIP_PRIVATE_KEY`、値は hex 文字列)。
ファイルのパーミッションは POSIX では 600、Windows では所有者のみに制限する。

## CLI の使い方

```bash
cd tools/dtcpip-rescue

# 録画一覧 (資格情報なしで動く)
python3 rescue.py list

# 1 件だけ取得 (ID は list の出力の content_id)
python3 rescue.py fetch --id <content_id>

# 全件取得
python3 rescue.py fetch-all
```

SSDP が届かない経路 (VPN 越しなど) では、アドレスを手動指定する
(環境変数 `RESCUE_HOST` でも指定できる):

```bash
python3 rescue.py list --host 192.168.11.129
python3 rescue.py fetch --id <content_id> --host 192.168.11.129
```

- 既に manifest にある録画はスキップされる。再取得は `--force`。
- `--timeout <秒>` で 1 件あたりの転送タイムアウトを指定できる (0 = 無制限)。
- `--fix` を付けると、取得に成功した録画を続けて `ts_fix.py` で変換し、
  `out/fixed/` に書き出す (既定はオフ。詳細は下の「番組情報の補完」)。
  既に取得済みでスキップされる録画にも変換だけは行われる。
  変換に失敗しても救出自体は成功として扱われ、失敗は分けて表示される。
  manifest と `SHA256SUMS.txt` は変換前のファイルの記録のまま変わらない。
- 取得結果の検証は `out/SHA256SUMS.txt` で行える:

```bash
cd out && sha256sum -c SHA256SUMS.txt
```

## 転送時間の目安

有線 LAN で実測した目安 (2026-11-17)。転送速度はサイズ・種別 (DR / 3倍) に
よらず約 11 MB/s で、AKE は約1秒。他に、一覧取得と取得後の検証
(SHA-256・マニフェスト更新) で 15〜20秒程度がかかる。
所要時間の目安は「サイズ ÷ 11 MB/s + 約20秒」。

| 種別 | サイズ | 転送 | 合計 (呼び出し全体) |
|---|---|---|---|
| DR | 約3.0 GB | 約4分35秒 | 約5分 |
| DR | 約1.6 GB | 約2分26秒 | 約2分45秒 |
| 3倍 | 約577 MB | 約52秒 | 約1分10秒 |

計測の詳細は [docs/49](../../docs/49_dtcpip_rescue_tool_and_windows.md) §7 を参照。

## 番組情報の補完 (ts_fix.py)

救出した TS は、再生はできるが、チャンネル名・番組名・放送日時などの
番組情報を読み取るツール (SDT / EIT / TOT を読むもの) からは
「情報なし」に見える。nasne が録画を 192 バイトパケットの部分 TS として
保存しており、番組情報が SIT という別の表にしか入っていないためである。

`ts_fix.py` は、これを地上波・BS の放送と同じ形 (188 バイトパケット、
NIT / SDT / EIT / TOT あり) の TS に変換する。映像・音声・字幕の
パケットには手を加えない。追加の依存はない (Python 3.10+ の標準ライブラリのみ)。

```bash
cd tools/dtcpip-rescue

# out/fixed/ に変換版を書き出す (既定の出力先は「入力と同じフォルダの fixed/」)
python3 ts_fix.py out/*.ts

# 出力先を指定する
python3 ts_fix.py out/*.ts -o /path/to/converted
```

- 入力ファイルは変更しない。変換中は `<名前>.partial` に書き、完了後に
  リネームする。
- 出力先に同名ファイルがあればスキップする。上書きは `--overwrite`。
- 変換済みの TS (188 バイト) を入力しても、同じ内容が再生成されるだけで壊れない。
- manifest・`SHA256SUMS.txt` と、`.ts.json` の `fetched` にある SHA-256 は
  **変換前** のファイルの値である。変換版はこれらと一致しない。
- 救出と同時に変換した場合 (CLI の `--fix` / GUI のチェックボックス) は、
  `.ts.json` に `fixed` フィールドが追加され、変換版の場所 (`fixed/…`) と
  その SHA-256 が記録される。
- 所要時間はほぼディスクの速度で決まる。目安は 1 GB あたり 5 秒前後。

部分 TS の構造と変換の仕組み、検証の記録は
[docs/53](../../docs/53_nasne_partial_ts_and_ts_fix.md) を参照。

## GUI の使い方

GUI は CLI と同じ処理を画面から実行するためのもので、追加の依存はない
(Python 標準の Tkinter のみ)。前提 (pydtcpip の用意と資格情報ファイルの配置)
は CLI と共通。

### 起動

```bash
cd tools/dtcpip-rescue
# Linux / macOS
python3 rescue_gui.py
# Windows
py rescue_gui.py
```

- Linux ではディストリビューションによって `python3-tk` パッケージが
  別途必要な場合がある。
- macOS でも同じコードで動作する。GUI の起動と、CLI の手動指定での
  列挙は Mac 実機で確認済み (詳細は docs/49 §8)。

### 画面の構成

- **資格情報ファイルの欄**: `credentials.env` のパス表示と、配置状態の表示。
  「変更…」でファイルを選んでパスを切り替え、「フォルダを開く」でそのフォルダを
  エクスプローラ / Finder / ファイルマネージャで開く。
- **nasne アドレス (手動指定) の欄**: SSDP の自動発見を使わず、nasne の
  アドレスを直接指定する。空欄なら自動発見。VPN 越しなど SSDP の
  マルチキャストが届かない経路で使う。入力した値は保存され、次回起動時も
  そのまま使われる。
- **「接続」ボタン**: 入力したアドレス (空欄なら自動発見) で、実際に
  nasne へ接続できるかを確かめる。結果は下のボタン行の右の表示に出る
  (「接続済み: サーバー名 (アドレス)」/「未接続: 理由」)。
- **「録画一覧を取得」ボタン**: nasne を発見し、録画一覧を表に表示する
  (資格情報は不要)。接続状態の表示は「接続」ボタンと共用。
- **録画一覧の表**: タイトル / 日時 / チャンネル / 長さ / サイズ / 残コピー。
- **「選択した録画を救出」ボタン**: 表で選択した録画 (複数選択可) を取得する。
- **「番組情報を読める TS に変換する」チェックボックス**: オンにすると、
  救出に成功した録画を続けて変換し、`out/fixed/` に書き出す
  (既定はオフ。詳細は「番組情報の補完 (ts_fix.py)」)。
- **進捗バー**: 転送中、取得済みサイズ / 全体サイズとパーセント、
  転送速度 (MB/s) を表示する。
- **救出済み (manifest) の表**: 保存済みの一覧 (タイトル / ファイル / サイズ /
  SHA-256 先頭 16 桁 / 検証結果)。「ハッシュ検証」ボタンで保存済みファイルの
  SHA-256 を manifest と再照合する。
- **ログ欄**: 処理の進行と結果が表示される。

### 操作の流れ

1. 起動したら、まず上部の **配置状態** を確認する。未配置の場合は、
   表示されているパスの場所に `credentials.env` を置く
   (「フォルダを開く」がそのフォルダを開く)。置き場所を変えたい場合は
   「変更…」でファイルを選ぶ。
2. 「録画一覧を取得」を押す (SSDP が届かない環境では、先に nasne
   アドレスの欄へアドレスを入れておく)。アドレスを入れた場合は
   「接続」を押すと、繋がるかどうかを先に確かめられる。
3. 表から救出したい録画を選び、「選択した録画を救出」を押す。
   進捗バー (速度表示つき) とログで進行を確認する。
4. 完了すると manifest の表に結果が追加される。必要なら「ハッシュ検証」で
   保存内容を再確認する。

### 配置状態の見方

配置状態は、ファイルの有無とサイズだけから判定している (値は読まない)。

- `未配置 (ファイルがありません)` — 指定パスにファイルがない。
- `未配置 (空ファイルです)` — ファイルはあるが 0 バイト。
- `未配置 (空テンプレートのまま)` — ファイルはあるが、空テンプレートと
  まったく同じサイズ (未記入のコピー)。
- `配置済み` — テンプレートとサイズの異なるファイルがある
  (2行だけの最小構成で手書きした場合も配置済みになる)。

### 注意点

- GUI も CLI と同じく、鍵の値を読まない・出さない・検証しない。
- 取得の実行中は、一覧取得と取得のボタンが無効になる。
- 既に manifest にある録画はスキップされる。再取得したい場合は CLI の
  `fetch --force` を使う。

## Windows での注意

- 日本語タイトル等が文字化けしないよう、ツールが子プロセスを UTF-8 で
  処理させる (仕組みは docs/50 §5.3)。
- PowerShell 5.1 のリダイレクトは UTF-16LE で書かれるため、`list` の JSON を
  ファイルに残す場合はエンコーディングに注意する
  (例: `Out-File -Encoding utf8` を使う)。
- pydtcpip の venv は Windows の `Scripts\` レイアウトに対応している。
