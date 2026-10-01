# D3接近：モデル相当の遅延と進入許可の更新

CPUのPX4/Gazeboシミュレーション1回で、D1・D2・D3、配送、帰船まで完了しました。
D3では最初の進入許可から85.20秒後に、
新しい空き観測に基づく許可を取得しました。30秒を超えた古い許可での進入は行っていません。

この試験は実VLA・WAMの代わりにfixtureと待ち時間を使っています。
実モデルのD3判断能力、AIによる待機判断の改善、実機飛行を示すものではありません。

## 条件

- Run ID: `yokohama-c1b1328b77fb`
- 確認開始時のコード: `3e80aa450222aa84326154775be5b458ad65f306`
- `--decision-backend fixture --pad-approach-decision --fixture-cold-start`
- 起動170秒、VLA10秒、WAM55秒の待ち時間。D1・D2・D3で短区間を移動。
- 静止船・無風・台本に沿って動く先行機。空きの判断は位置情報を使うRulesと決定的なMissionOS fixture。
- CPUのAI助言は使わず、追加GPU費用は$0。
- 前回の遅延試験 `yokohama-27900a389aae` はD2で停止した開発試験として残しています。
  今回は提案の移動量をモデル入力時の観測位置から測る修正後の確認です。

## 観測した順序

以下はworkerの開始を基準とする実時間 `wall_s` です。シミュレーション時間とは区別します。

| 観測 | 実時間（秒） |
|---|---:|
| パッド占有を報告 | 917.35 |
| 空きを確認して最初の進入許可を取得 | 950.04 |
| 推論待ち後、D3移動前に許可を再取得 | 1035.25 |
| D3の短区間移動に到達 | 1045.12 |
| 到達先で再観測し、配送接続前に許可を再取得 | 1050.78 |
| 荷物の受領を確認 | 1191.75 |
| 船上への着陸・disarmを確認 | 1774.33 |

再確認はどちらも5秒以上の連続した空き観測を使っています。
D3移動のdispatch確認時の許可年齢は3.45秒、
配送接続時は3.25秒でした。両方とも直近の許可IDと一致しています。

記録されたD3の同じ観測を実際の `PadQueue.require_dispatch` に再入力したCPU再検証でも、
最初の古い許可は `Missing or expired pad entry permission` で拒否され、
再取得した許可は受理されました。この再検証では飛行命令を送りません。

## 実測した処理待ち時間

requestからresponseまでの実時間です。モデル推論の実測値ではありません。

| 判断回 | 処理 | 実時間（秒） |
|---:|---|---:|
| 1 | start | 170.04 |
| 1 | vla | 10.17 |
| 1 | wam | 55.63 |
| 2 | vla | 10.19 |
| 2 | wam | 55.75 |
| 3 | vla | 10.16 |
| 3 | wam | 55.58 |

## 検証

- `decisions`: passed（10項目）
- `pad_queue`: passed（23項目）
- `payload`: passed（10項目）
- `sitl`: passed（21項目）

許可の期限切れ、更新、dispatchとのID対応、実測待ち時間を含む
追加確認も11項目すべて合格しました。
[確認結果と証跡ハッシュ](qualification.json)に数値を保存しています。
元の飛行・画像記録は実験保管領域に保持し、この公開資料には集計とハッシュを収録しています。

```sh
python scripts/yokohama_sitl.py --phase flight --approve-sitl --output-dir RUN \
  --sea-round-trip --deliver-payload --occupied-pad --decision-backend fixture \
  --wam-profile motion-v4 --pad-approach-decision --fixture-cold-start \
  --timeout-seconds 3000
python scripts/verify_yokohama_decisions.py RUN --output RUN/codex-verification-decisions.json
python scripts/verify_yokohama_pad_queue.py RUN --output RUN/codex-verification-pad_queue.json
python scripts/verify_yokohama_payload.py RUN --output RUN/codex-verification-payload.json
python scripts/verify_yokohama_sitl.py RUN --output RUN/codex-verification-sitl.json
```

既存の先行機再進入試験 `yokohama-ddffc5f8c171` も現行のfault verifierで再確認し、
D3推論待ち中の再占有、D3移動・配送の未送信、セッションとシミュレータの終了の7項目が合格しました。
これは今回の成功飛行とは別の記録の再検証です。

任務全体でのVLA/WAMの役割と制約は[設計資料](../../agents/yokohama-pad-native-approach.md)に記載しています。
