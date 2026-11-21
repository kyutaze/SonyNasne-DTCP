# 51. pydtcpip v1.0.0 の nasne 実機検証と Move finalize 修正 (PR #25)

## 1. 位置づけと範囲

- 本書は、公式 pydtcpip **v1.0.0** (2026-11-18 公開) を公式 FW の nasne 実機で検証し、見つかった Move の欠陥を修正して上流へ PR を投稿した記録である。検証の分担と資格情報の扱いは [docs/48](48_dtcpip_rescue_pipeline.md)・[docs/50](50_dtcpip_rescue_tool_internals.md) の作法どおり (鍵は本人が `credentials.env` にだけ配置し、検証側のプログラムは値を見ない。消費は pydtcpip 本体のみ)。本書にも鍵の値・個体を特定できる値 (IP・MAC・シリアル等) は書かない。
- 背景: pydtcpip 作者が「手元にないレコーダーの互換性問題は Issue だけでは対応できない。LLM エージェントに調べさせるなら自環境で動くパッチを作って PR まで投げてほしい」旨の方針を README に明記した。うちの従来検証は v0.1.0 時点 ([docs/32](32_pydtcpip_survey.md)・[docs/37](37_pydtcpip_live_verification.md) の互換性調査と互換性レポート Issue #16、救出成立は docs/48・49) であり、本検証はその v1.0.0 追随である。
- 検証範囲は本人の指示で **pydtcpip v1.0.0 の全機能** (discover / info / dump / stream / move / resume / receive / tui)。move の検証対象には、当時進行中だった 200 件録画キャンペーン (torne mobile HB Issue #1 の実測準備) の埋め草タイトルを使ってよいこと、消費したタイトルと件数変動を記録することが条件とされた。予約 (XSRS) は pydtcpip の機能ではないため対象外。

## 2. v0.1.0 → v1.0.0 の主な変更点 (nasne に関係する範囲)

- **プロファイル統合**: 従来の `sie_nasne` は Sony・Buffalo 共通の **`nasne`** プロファイルに統合された (modelName が nasne / buffalo-nasne / buffalo-nasne2 に一致)。discover での自動選択はこのプロファイルになる。
- **COGCount の解釈変更**: 残りコピー数は「生値 − 1 (最終 Move 分)」に正規化されて表示される。生値 10 のタイトルは 9 と表示される。
- **CLI 構成**: discover / info / move / resume / tui / stream / receive / dump。`dump --mask-private` (識別子・番組情報のマスク) が追加され、TUI が新設された。
- **資格情報**: data ディレクトリ (`~/.local/share/pydtcpip/`) の `credentials.env`、環境変数 (優先)、`--credentials` の3経路。本検証では救出用の既存 env ファイルを `--credentials` で指して使った。
- **SRM**: data ディレクトリへ `srm.bin` として自動保存される。
- 要 Python 3.13 以上 (検証環境はチェックアウト同梱 venv の Python 3.15.2 で実行)。

## 3. 実機検証の結果 (修正前)

| 機能 | 結果 |
|---|---|
| discover | OK。プロファイル `nasne` が自動選択される |
| info | OK。番組詳細・チャプタ・COGCount 等の追加照会も取得できる |
| dump --mask-private | OK。description / SCPD / browse / 録画メタデータ / HEAD 応答がマスク付きで出力される |
| stream | OK。359,933,376 バイト (info の申告サイズと完全一致)、転送 11.1 MB/s、AKE 完走、SRM v2 受領、E-EMI 0x4、MPEG-TTS 同期を 3000 パケット標本で確認。SHA-256 `aa3501a0f05e32550b6aea91889f2e8dd425757852dbb5d414470ec60061f59b` |
| move | **NG (本書の主題)**。データ転送は完走するが MV_FINALIZE への応答が無く、接続が切れて Move が確定しない。コピーは消費されない |
| resume | NG (同因)。残存状態に対して新規に認証した接続で finalize しても無応答 |
| receive | 起動・SSDP 応答・description 提供まで確認。実際のダビング開始はレコーダー側 UI で宛先を選ぶ操作が必要で、本環境からは駆動できない (到達範囲の境界) |
| tui | OK。起動し、発見した nasne が一覧に表示される |

## 4. Move finalize 欠陥の切り分け

### 4.1 症状の再現

- 公式 CLI の `move` を2タイトルで実行し、いずれも転送完了後に finalize が無応答で失敗した。失敗後もソース側のコピー数は不変で、遅延・非同期の確定も起きなかった (再照会で確認)。
- 上流にも同一症状の報告 **Issue #21** (maguso、2026-11-18、Windows、CECH-ZNR1J、FW 2.60) があり、パケットキャプチャで「AKE 完了の約 11 ms 後に nasne が FIN を送って AKE 接続を閉じる。finalize 時に再接続するローカル修正で動いた」と報告されていた。

### 4.2 プローブによる原因確定

資格情報は従来どおり env 経由で pydtcpip 本体にだけ消費させ、制御フレームの送受信だけを替えたプローブで切り分けた。

- **プローブ C (素の TCP 接続で finalize 再試行)**: 認証を一切しない新規 TCP 接続では、nasne は Move 制御フレームを解釈して応答する。12 分前の状態には REJECTED (状態なし) を返し、4 分前の状態には **ACCEPTED (MAC 検証成立) + MV_COMPLETE 完走**を返して、ソースのコピー数が実際に 1 つ減った。
- **認証済み接続では 0/3 で無応答**: 通常認証・Move 認証のどちらの新規接続でも finalize は黙って切断された。#21 のキャプチャ所見 (認証完了直後に FIN) と整合する。つまり nasne は **認証が完了した接続を即座に閉じる**ため、その接続に書いた finalize は届かない。
- **プローブ D (失敗直後に素の接続で再試行)**: 死んだ接続へ finalize を試行した直後の素の再試行も切断された。失敗した試行が直後の再試行を道連れにする (機序は未確定。接続の後片付け待ち・制御接続の直列化が候補)。
- **プローブ E (決定的検証)**: `client.move()` を鏡写しにし、トランザクションの生成時点で接続を持たせず、転送直後の **最初の finalize を新規の素の接続で**送る構成にしたところ、295,001,472 バイトの Move がエンドツーエンドで確定した (SHA-256 `66b2e9c61c9c23540021f29ffc9294ddce2a65a6d681761b32293120f21dcf7b`、同期確認済み、コピー数 −1)。

結論: 欠陥は「finalize を送る接続の形」にある。仕様上 finalize は新規 TCP 接続でよいとされており、nasne はその形 (未認証) でのみ応答する。逆に DBR-Z620 は未認証の finalize を閉じる (既存コードのコメントに明記) ため、どちらか一方の形に固定はできない。

## 5. 修正内容 (PR #25)

- PR: https://github.com/tsukumijima/pydtcpip/pull/25 (フォーク mar1mo-41414/pydtcpip、ブランチ `move-finalize-reconnect`、コミット `655fab9`)。
- プロファイルでは表現できない「Move トランザクションの接続処理」の問題であるため、修正はコア側に入れた。理由は PR 本文にも明記した。
- 変更は3点:
  1. `pydtcpip/dtcp/move.py` `_exchange`: 保持している接続が既に EOF (相手が閉じた) なら、要求を書いて失う前に接続を捨てて再接続する。
  2. `move.py`: finalize / complete の交換を、応答が失われた場合に **同一 nonce のまま新規接続で再試行**する (上限3回、間隔 1 秒 / 4 秒)。これは MoveTransaction の docstring が既に掲げていた「キャンセルせず再試行可能に保つ」意図の実装である。**拒否 (REJECTED) は definitive な答えとして再試行しない**ため、結果不明・拒否済み resume の既存意味論は不変。
  3. `pydtcpip/client.py` `_move_control_connector`: 再試行用の接続を **認証あり → 未認証 → …と交互**にする。初回は認証ありで従来の DBR-Z620 挙動を保存し、nasne 型 (認証接続を閉じる機種) では次の未認証接続で届く。

## 6. テスト

- 全スイート **304 件合格**。`ruff check` / `ruff format --check` / `pyright` はいずれもクリーン。
- 既存テスト4件は「finalize / complete の応答を1回失うと Move が中断し resume に委ねる」という旧意味論を固定していたため、新仕様に合わせて更新した: 応答を全試行分 (3回) 失う形に変えて resume 経路の被覆を維持し、帯域内再試行で Move が完走する新テスト `test_move_retries_finalize_after_response_is_lost` を追加した。対象は tests/test_client.py (3件) と tests/test_metadata.py (1件)。

## 7. 修正後の実機再検証

- **公式 CLI `move` (パッチ適用版) が1コマンドで完走**した。対象は埋め草タイトル「天気予報」(558,030,144 バイト)。ログには再試行の発動 ("No response to the Move control request (command: 0x22); reconnecting to send it again.") → finalize 受理 → "Move completed." が残り、出力の MPEG-TTS 同期も確認した。SHA-256 `58e28f8371aef401a51f45cfdf477c72d65e4010e317a8dfbb70b7c34fb974fd`。ソースのコピー数は表示値 9 → 8 (生値 10 → 9) とちょうど1つ減った。
- **CLI `resume` (パッチ適用版)** を約35分前の残存状態に対して実行: 認証接続は切断され、再試行 (未認証) で **REJECTED (状態失効)** を受領し、definitive として再試行せず、既存の「結果不明」メッセージとともに受信データを保持して終了した。設計どおりの経路である。

## 8. 消費したタイトルと件数変動の記録

- プローブ C の ACCEPTED により埋め草タイトル1件 (id …20) が **1コピー消費** (生値 10 → 9)。タイトル自体は残存。
- プローブ E により埋め草タイトル1件 (id …17) が **1コピー消費** (生値 10 → 9)。タイトル自体は残存。
- パッチ版 CLI move により「天気予報」(id …14) が **1コピー消費** (生値 10 → 9)。タイトル自体は残存。
- 修正前の失敗した move 2件 (id …24 / …10) はコピーを消費しなかった。
- いずれもタイトル件数そのものは減っていないため、200 件キャンペーン (タイトル数を目標に補充 cron が回る) への影響はない。

## 9. 残件

- PR #25 のマージ可否は上流の判断待ち。
- 互換性レポート Issue #16 へ PR への相互参照コメントを投稿した (§10)。Issue #21 へは PR 本文からの参照で自動的に相互リンクが付く。
- receive の実転送 (レコーダー側 UI 起点のダビング) は未検証のまま。駆動できる UI 経路が用意できたら検証する。

## 10. Issue #16 への相互参照コメント (2026-11-19)

- 本人の判断で、互換性レポート Issue #16 に相互参照コメントを投稿した: https://github.com/tsukumijima/pydtcpip/issues/16#issuecomment-6994279457
- 内容: v1.0.0 での再検証結果 (discover / info / dump / stream / tui が無修正で成立、move に欠陥あり) と、修正 PR #25 へのリンク。
- これにより §9 の「相互参照コメントを足すかは未決定」は解消した。

## 11. CodeRabbit レビューへの対応 (2026-11-20)

- PR #25 に CodeRabbit (自動レビュー) から actionable コメントが2件付いた。いずれも実コードと突き合わせて妥当と判定し、修正を fork ブランチへ push した (PR に自動反映、head は `e0b1f93`)。各コメントへ対応内容の返信を投稿した。
- **指摘1 (Major、finalize 再試行時の結果不明の記録)**: 帯域内再試行が最初の応答消失を内部で捕捉するため、再試行が明示的に拒否されても `finalize()` の例外処理は実行されず、`is_finalize_result_unknown` が `False` のままだった。その結果、既に確定した可能性がある取引が `may_be_committed=False` として報告され、失敗時の後処理 (`abort_move`) でも受信済みデータが破棄され得た。修正コミット `4943d38`: `_exchange_with_reconnect` が MV_FINALIZE の応答消失を捕捉して再試行に進む時点で結果不明を記録する。最初の要求が明示的に拒否された場合は再試行自体が行われないため、従来どおり `False` を維持する。返信: https://github.com/tsukumijima/pydtcpip/pull/25#discussion_r4637404427
- **指摘2 (Minor、別コマンド応答時の接続の扱い)**: `_exchange` の別コマンド応答の判定は、失敗した接続を閉じる `try` の外にあったため、その経路では接続が残り、次の再試行が不整合な接続へ送信していた。修正コミット `e0b1f93`: 再試行ループ側ではなく検出箇所の `_exchange` 内で接続を閉じる形にした (失敗した接続は破棄するという `_exchange` の既存の意図に揃え、再試行を経ない `cancel()` 等の呼び出し元でも効くようにするため)。返信: https://github.com/tsukumijima/pydtcpip/pull/25#discussion_r4637404663
- **テスト**: 回帰テスト2件を追加した (フェイクに `reject_finalize_after_accepts` / `wrong_command_finalize_responses` のノブと接続数カウンタを追加)。2件とも修正前のコードでは失敗し、修正後に通ることを確認した。全スイート **306 件合格**、`ruff check` / `ruff format --check` / `pyright` クリーン。コミット `4943d38` 単体のツリーでも tests/test_client.py の全件合格を確認した。
- **実機再検証 (未完)**: 修正が finalize 再試行に触れるため、埋め草タイトルで move の再検証を試みたが、当日は機体側が finalize に応答しない状態で完走しなかった。修正版で2件 (id …126「ニュース・気象情報」、id …102「ワンポイント手話」) とも転送は完走したが finalize の全試行 (認証あり→未認証の交互) に応答が無く、うち …126 は保持された resume 状態からの再開でも拒否 (状態失効) となった。A/B として修正前コード (`655fab9`) でも1件 (id …104) を試みたが、こちらはメディア GET 自体が HTTP 514 で拒否された。**コピー消費はゼロ**で、対象タイトルの表示コピー数は前後とも 9 のまま、タイトルも残存している。修正前コードでも同様に失敗することから、当日の失敗は本修正の回帰ではなく機体側の状態 (キャンペーン録画中の負荷等) によるものと判断した。受信済みデータは `.part` (505,022,592 バイト / 588,466,368 バイト) として作業場所に保持されている。move 完走の再検証は、機体が落ち着いたタイミング (200 件キャンペーン完了後) に改めて行う。

## 12. CodeRabbit 対応後の実機再検証 (2026-11-21、完了)

- §11 で未完だった move 完走の再検証を、200 件キャンペーン完了後 (タイトル 199 件、録画・転送の負荷がない状態) に実施した。手順は SonyNasne-TryDebian 側の 2026-11-21 セッション手順書の段4による。
- チェックアウトはフォーク `move-finalize-reconnect`、head `e0b1f93` (§11 の修正2コミットを含む最終形) であることを `git log` で確認した。資格情報は救出用の既存 env ファイルを `--credentials` で指して使った (値は従来どおり見ていない)。
- 対象は埋め草タイトル1件 (id …202、135 秒、cleartextSize 190,781,568 バイト、コピー数 生値 10 / 表示 9)。
- **公式 CLI `move` が1コマンドで完走した。** 転送完了後の最初の finalize (コマンド 0x22) は認証済み接続で無応答となり、修正の再試行が発動 ("No response to the Move control request (command: 0x22); reconnecting to send it again.") して、新規 (未認証) 接続で finalize が受理され、続く MV_COMPLETE (0x23) も受理されて "Move completed." で終了した。再試行の発動は1回だけだった。
- 受信データは 190,781,568 バイトで cleartextSize と完全一致した。MPEG-TTS の同期 (192 バイト周期、+4 オフセットの同期バイト) を全 993,654 パケットで確認した。SHA-256 `53d87004c63043d8e8eab8372c290ebd990790f4feb6507f13b477186b772b43`。
- ソースのコピー数は生値 10 → 9 (表示 9 → 8) とちょうど1つ減り、タイトル自体は残存し、タイトル総数も前後とも 199 件で不変だった。
- 結論: **§11 の 2026-11-20 の失敗は、キャンペーン録画中の機体側の状態によるもので、CodeRabbit 対応 (4943d38 / e0b1f93) の回帰ではない**ことが確定した。PR #25 の move は最終形で実機完走を再確認でき、§11 の「実機再検証 (未完)」はこれで解消した。残件は §9 のとおり PR のマージ可否 (上流の判断待ち) のみである。
