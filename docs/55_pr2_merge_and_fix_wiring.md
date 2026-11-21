# 55. PR #2 (ts_fix) の取り込みと、救出後変換の選択式配線

## 0. 要約

- 本人決定 (2026-11-20): PR #2 をマージし、変換を「救出後に変換する・既定はオフ」の選択式でツールに組み込む。docs/54 §6 で提案した 2 段階 (そのまま取り込み → 選択の配線を後続で追加) を実施した。
- **マージ**: merge commit `fff6b2a`。GitHub 上は CONFLICTING 表示だったが、実競合は docs/README の索引行 1 箇所のみで、53 / 54 両方の行を残して解消した。コード本体はクリーンにマージされている。
- **配線**: commit `adca70b`。CLI の `--fix` (fetch / fetch-all) と GUI のチェックボックス「番組情報を読める TS に変換する」(既定オフ)。変換版は `out/fixed/` へ出し、所在と SHA-256 はサイドカー `.ts.json` の `fixed` フィールドに記録する。manifest / SHA256SUMS は変換前の記録のまま変えない。変換の失敗は救出の失敗とは分けて報告する。
- **検証**: 実機 (公式 FW の nasne、200 件ためキャンペーン中) で CLI の両経路と GUI の実走を確認した。使う操作は列挙と `stream` 取得のみで、コピーは消費していない。詳細は §3。

## 1. マージ

- PR #2 (hydh、head `ff93813`) は docs/54 のレビュー時点で main に docs/54 が追加されていたため、GitHub 上 CONFLICTING になっていた。`git merge-tree` で確認した実競合は `docs/README.md` の索引行のみ (PR が 53 の行を、main が 54 の行を、同じ位置に追加していた)。`ts_fix.py`・ツール README・docs/53 は競合なし。
- 解消は索引行を 53 → 54 の順に両方残すだけとし、ローカルでマージコミット `fff6b2a` を作って push した (PR #1 と同じくマージコミット方式で貢献者のコミット `ff93813` を保持)。push 後、PR は MERGED になった (mergeCommit `fff6b2a`)。
- マージ直後の差分は PR の内容そのもの (+626 行) で、`ts_fix.py` のコンパイルも通る。

## 2. 選択配線の実装 (commit `adca70b`)

### 2.1 rescue.py

- `Config.fixed_dir` (`out/fixed/`) を追加。
- `convert_fetched(config, filename)`: `ts_fix.Converter` を import して変換する。変換版が既にある場合は再変換せず、所在と SHA-256 の記録だけを最新化する (冪等)。失敗時は ts_fix が残す `.partial` を片付け、`fix FAILED: ... -- the rescued file is unaffected` と分けて報告し、例外を呼び出し側の成否に伝播させない。
- `_record_fixed()`: サイドカー `.ts.json` に `fixed` ブロック (`file` / `bytes` / `sha256` / `at` / `tool`) を追加する。サイドカーの `fetched` と manifest は従来どおり変換前のバイト列の記録のまま (docs/53 §3.3 の判断に沿う)。
- `fetch_one(..., fix=False)` を追加。取得に成功した場合に加え、既に取得済みでスキップされる場合も変換だけを行う (再取得なしで既存の救出物を変換できるようにするため)。
- CLI は `fetch` / `fetch-all` の両方に `--fix` を追加。

### 2.2 rescue_gui.py

- ボタン行にチェックボックス「番組情報を読める TS に変換する」を追加 (`BooleanVar`、初期値オフ)。`_start_fetch` で値を読み取り、`fetch_one(fix=...)` へ渡す。変換の進行はログ欄の `fix:` 行で分かる。

### 2.3 ツール README

- CLI の使い方に `--fix` の説明 (既定オフ、スキップ時も変換だけ行う、変換失敗は救出と分けて扱う、manifest は不変) を追加。
- ts_fix の節に、救出と同時に変換した場合は `.ts.json` に `fixed` フィールドが付くことを追記し、「SHA-256 は変換前の値」の記述を `fetched` の値である旨に正確化。
- GUI の画面構成にチェックボックスの項目を追加。

## 3. 検証 (2026-11-20、marnux + 実機)

対象は公式 FW の nasne (192.168.11.129、200 件ためキャンペーン中)。列挙と `stream` 取得のみで、コピーは消費していない。対象タイトルは列挙時点 (125 件) の最小のもの: content_id `17950996090000000091`「２０２６ＮＨＫ杯フィギュア　坂本花織が語るＮＨＫ杯フィギュアの魅力」(54 秒、94,068,288 B)。作業場所は `SonyNasne-DTCP.work/2026-11-20-pr2-fix-verify/` で、検証後に `.ts` ペイロードは削除し、ログ・JSON・ハーネスを保存した。

### 3.1 CLI、`--fix` なし (従来経路)

- 従来どおり `.ts` + `.ts.json` + manifest + SHA256SUMS ができる。`fixed/` は作られず、サイドカーに `fixed` キーは無い。
- 取得ファイルの SHA-256 は `8a6f220cb605143e48d766ca618cf5cbbe9bedbfcf060ddec78b6caee460185b`。

### 3.2 CLI、`--fix` あり

- 取得ファイルの SHA-256 が §3.1 と完全一致 (`8a6f220c…`)。従来経路の出力は変わらない。
- `out/fixed/` に変換版ができた (92,157,600 B、SHA-256 `5094746ba60217336b987b5472718eeea3f3f641a3c1ec2cfffb6491b487f1d9`)。変換統計は入力 489,939 パケット / 追加 261 / SIT 57。
- サイドカーに `fixed` ブロックが入り、その `sha256` が変換版の実測と一致した。manifest と SHA256SUMS は変換前ファイルのみを指し、§3.1 と同一内容だった。
- 構造検証 (docs/54 §5 の検証スクリプト `validate_fixed.py` / `kept_packets.py` を再利用): 全セクション CRC 一致 (不一致 0)、PAT のネットワーク PID = 0x10、合成 PID の連続性カウンタの抜け・重複 0、4 表すべて出現 (NIT 6 / SDT 28 / EIT 108 / TOT 11)。保持パケット (映像・音声・字幕・データ・PMT・SIT 等) は全 15 PID で入力と完全一致。
- ffprobe: 変換前は program / service_name が無いが、変換版では service_name が読めた (値は ARIB 符号のまま。ARIB の復号は ariblib 等の担当で、貢献者はそちらで確認済み)。
- 再実行 (`--fix` あり、取得済み): `skip (already fetched)` + `fix: already converted` で終了コード 0、サイドカーの `fixed` は同じ値で再記録された。

### 3.3 変換失敗の分離 (単体確認)

- TS でないファイルを既取得に見立てて `fetch_one(fix=True)` を呼ぶ単体確認を行った。`fix FAILED: ... (not a 188/192-byte TS) -- the rescued file is unaffected` が分けて表示され、`fetch_one` は True (救出成功) を返し、`.partial` は残らず、manifest は作られなかった。変換失敗が救出の成否・終了コードに影響しないことを確認した。

### 3.4 GUI (Xvfb 実走)

- ハーネス (`gui_harness.py`) で実 GUI を起動し、チェックボックス「番組情報を読める TS に変換する」が存在することと、初期値がオフであることを確認した。
- 実機の列挙 (125 件) を GUI 経由で取得し、対象タイトルを選択してチェックボックスをオンで救出を実行した。取得・変換とも成功し、取得ファイルと変換版の SHA-256 が CLI の結果 (§3.1・§3.2) と完全一致した。サイドカーの `fixed` も一致。

## 4. お礼コメント

- PR #2 に投稿した: https://github.com/mar1mo-41414/SonyNasne-DTCP/pull/2#issuecomment-7027227660 (検証内容と、選択式で組み込んだ旨を含む)

## 5. 残件・注記

- docs/54 §3.3 の軽微な指摘 5 件 (SIT のない TS を入れた場合の警告など) は未対応のまま。取り込みの阻害要因ではなく、将来の改善候補として残す。
- CS 録画での変換は引き続き未検証 (docs/53・54 のまま)。
- `fetch-all --fix` の大量一括変換は未実施 (経路は fetch と共通で、1 件の実走まで確認)。
- docs/50 (ツール内部) の fetch の流れの記述には、任意の後処理として本書の変換工程が加わった。
