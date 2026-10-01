# 風を加えた配送試験とバッテリー表示

離陸後に風を加えるCPU試験で、2 m/s設定の配送・帰船を検証しました。 1機・静止船・固定建物・一定東風の限定条件です。風の力はGazeboの近似式を使っており、実機の耐風性能を測った値ではありません。

同じ無風の実VLA＋WAM飛行に、[残量・電圧付き動画](../yokohama-cargo-flight/battery/battery-flight.mp4)を追加しました。[動画と3Dリプレイ](../yokohama-cargo-flight/index.html#battery-video)・[風試験の動画と軌跡](index.html)。今回の追加GPU費用は0ドル。前回までの累計見積もりは16.7063 / 17ドル（請求額は未確定）です。

## 全試行

| 試験 | 風の設定と開始 | 結果 | 30秒AP保持 | 最終工程 | 記録時間 |
| --- | --- | --- | --- | --- | --- |
| wind-2 | 2 m/s・生成時から | 未達 | 0 / 13 | preflight | 89.0秒 |
| wind-2-airborne | 2 m/s・離陸・保持後 | 配送・帰船を検証 | 13 / 13 | return_land | 1469.5秒 |
| wind-5-airborne | 5 m/s・離陸・保持後 | 未達 | 1 / 13 | SEA-INBOUND-COAST | 507.5秒 |

wind-2-airborne: 荷物はパッド中心から0.721 m、3.012秒の静止を観測。

時間は最初から最後のテレメトリーまでのシミュレータ時刻差です。1条件につき1試行で、成功率や最大許容風速は確定していません。失敗を成功例で置き換えていません。5 m/s条件は2 m/sの全検証が合格した場合だけ実行する事前条件です。

最初の `wind-2` では、機体を生成してから着地・推定器初期化する間に風が作用し、機体のpitchが約67.1度になりました。PX4は姿勢不良として離陸を拒否し、90秒のpreflight待機で終了しました。風の力そのものは独立物体の移動で確認できています。

次に別条件 `wind-2-airborne` を事前に定義しました。無風で離陸して30秒保持し、その後に風を開始します。これは飛行中の外乱応答の切り分けで、風が吹く甲板からの離陸成功を意味しません。風を開始してからの保持・モデル区間到達・荷物受領の基準は維持しています。

5 m/s条件は海岸手前約122.9 mで、区間到達待ちの上限435秒（壁時計）を超えて終了しました。最後の30シミュレータ秒の速度中央値は約0.584 m/sでした。市街地の判断処理も荷物分離も始まっていません。実測記録から、5 m/sで全行程が成立したとは判断できません。[試行の結果](wind-5-airborne/summary.json)。

## 何を動かしたか

海上往復はAP。市街地D1・D2の再観測・提案・許可・移動・到達確認には**CPUの模擬VLA/WAM**を使いました。風ありの実VLA＋WAM推論、追加学習、新しいGPU利用はありません。無風の実モデル結果と集計を混ぜていません。

WindEffectsは機体のbase_linkと荷物のpayload_linkへ `質量 × (風速 − 物体速度) × 1/s` の力を加えます。既存のローター抵抗も残ります。風を受ける、重力と推進力のない観測用物体と、風を受けない対照物体を経路外に設けました。SDFの設定と実際の移動の両方を再検証します。風のサービス応答は設定値の確認で、局所風の計測ではありません。

原典: [実行版Gazebo 8.11.0のWindEffects](https://github.com/gazebosim/gz-sim/blob/gz-sim8_8.11.0/src/systems/wind_effects/WindEffects.cc)。突風、建物の乱流、波、船の動揺、較正済み空力は未実装です。生成時からの風の試験は失敗として残り、甲板への固定や風中の離着陸は今後の検証です。

初期試行のメタデータの `affected_links` には荷物のリンク名を `link` と記した誤記がありました。実際のSDFは `payload_link` で、風の有効化と検証はこの実要素に作用しています。生ログの誤記は書き換えず、公開summaryの `observed_cargo_link` に実名を記録しました。生成コードは修正済みです。

## バッテリー表示の範囲

映像には、各画像時刻以前の最新の `battery_status` 残量と電圧を表示します。古さ2秒を超える値、欠測、無効値は `UNAVAILABLE`。元画像のhashと時刻を照合し、表示欄を画像の上へ追加しています。元の映像は保持しています。

PX4の `battery_simulator` はarm中の経過時間を使った模擬電圧です。この設定は `SIM_BAT_DRAIN=3600`、下限 `SIM_BAT_MIN_PCT=0`。`current_a=-1` は未計測を示します。disarm後には模擬電圧が戻ります。Wh、実際の航続時間、風や機上VLA/WAMの電力消費は算出できません。[実行版PX4ソース](https://github.com/PX4/PX4-Autopilot/blob/381149fb012762f5e38c4a7fdc1b905b28038970/src/modules/simulation/battery_simulator/BatterySimulator.cpp)。

既存の無風・実モデル飛行の動画は798フレーム、211.166667秒（8倍速）。12フレームで新鮮な残量値がなく、欠測表示しています。風試験は別の映像と別の残量対応表です。

街区原典：横浜市・Project PLATEAU（2024年度公開カタログ）を加工 / CC BY 4.0。[出典とライセンス](../yokohama-urban-scene/ATTRIBUTION.md)。

## 再検証と境界

[全summary](summary.json)にはrun ID、world hash、実行ソースhash、生ログhash、全行程の検証結果を記録しています。各試験フォルダーの `wind-verification.json` は力の確認、`hold-results.json` は保持、`trajectory.json` は間引かない時系列位置です。成功試験では飛行・判断・荷物の3つの既存verifierを実行します。未完了試験を成功verifierへ通したと記しません。

既存の合格条件は13地点で30秒保持、水平誤差1 m以下、鉛直誤差0.6 m以下、速度0.5 m/s以下。モデル提案区間には別の厳しい到達基準があり、荷物は接触と3秒以上の静止、受領後の帰船・接地・disarmまでが必要です。これらはシミュレーションの観測条件で、実機の運航許可ではありません。

```sh
python scripts/yokohama_sitl.py --phase flight --approve-sitl \
  --output-dir "$RUNS/wind-2-airborne" --decision-backend fixture \
  --wam-profile motion-v4 --sea-round-trip --deliver-payload \
  --wind-east-mps 2 --wind-after-takeoff --timeout-seconds 2400
python scripts/verify_yokohama_sitl.py "$RUNS/wind-2-airborne" --output "$RUNS/wind-2-airborne/verification.json"
python scripts/verify_yokohama_decisions.py "$RUNS/wind-2-airborne" --output "$RUNS/wind-2-airborne/decision-verification.json"
python scripts/verify_yokohama_payload.py "$RUNS/wind-2-airborne" --output "$RUNS/wind-2-airborne/payload-verification.json"
python scripts/build_yokohama_battery_video.py --run "$NATIVE_RUN" --output "$NEW_VIDEO_DIR"
```

`RUNS` は新しい保存先、`NATIVE_RUN` は既存の実モデル飛行記録、`NEW_VIDEO_DIR` は未作成の出力先です。生成時からの風は `--wind-after-takeoff` を外し、別出力先で実行しました。5 m/s条件を実行した場合は速度だけを5に変更しています。プレビュー生成ソースは `scripts/export_yokohama_wind_report.py`、動画の生成ソースも各videoフォルダーに同梱しています。

次は、5 m/s条件でAPの進行が遅くなる原因を短い海上コースで切り分けます。実消費電力を比較するには、別途、機上の電流を含む計測が必要です。
