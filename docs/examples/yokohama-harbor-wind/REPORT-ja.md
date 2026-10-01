# 横浜港を想定した区間別の風

**想定風速を実装しました。基本条件の配送飛行は沖合6m/sで未達です。** 風を位置に応じて切り替える処理は別のGazebo試験で確認できましたが、今回の配送飛行は市街地まで到達していません。

[記録した軌跡と残量付き動画](index.html) · [診断数値](diagnostic.json) · [凍結した試験条件](protocol.json)

## 想定条件

ユーザー提供の目安をシミュレーションの入力条件として採用しました。横浜港の実測値・予報・飛行高度での風速を確認したものではありません。

| 区間 | 提示された目安 | 基本条件 | 上限寄りの条件 |
|---|---:|---:|---:|
| 沖合約1km | 5〜8m/s | 6m/s | 8m/s |
| 港内の開けた水面 | 4〜7m/s | 5m/s | 7m/s |
| 海沿い | 3〜6m/s | 4m/s | 6m/s |
| 市街地 | 1〜4m/s | 3m/s | 4m/s |

基本条件は実行済みですが、飛行中に訪れた風区間は沖合のみです。上限寄りの条件は設定済み・未実行。市街地の8/10/12m/sの突風は検討条件として記録した段階で、発生時間・継続時間・実装・試験は未実施です。

風向は前回と揃えて東向き（ENU +X）に固定しました。南西風の再現ではありません。飛行高度で一様とし、高度による増減は計算していません。

## 配送飛行の結果

対象は静止船・静止街区・50gの荷物・1機です。海上はAPのみ、市街地にはCPUの模擬判断を設定しています。実VLA/WAMとGPUは今回動かしていません。

| 項目 | 観測結果 |
|---|---|
| 試行 | 基本条件1回、`yokohama-29880f17cae8` |
| 終了 | `SEA-INBOUND-COAST`の到達待ち435壁時計秒でタイムアウト |
| 最終時刻 | シミュレーター512.604秒 |
| 海岸側目標まで | 718.02m |
| AP保持 | 1/13地点（離陸後の無風保持のみ） |
| 最後30シミュレーター秒の対地速度中央値 | 0.584m/s |
| 同じ期間のAP軌道目標速度中央値 | 0.582m/s |
| 市街地判断・配送・帰船 | 未達 |
| 追加GPU費用 | 0ドル（累計見積もり約16.71 / 17ドル） |

前回と同じ近似力の係数と到達・保持基準を使い、未達の結果を保持しました。無風の離陸・30秒保持のあとに風を加えています。風のある甲板からの離陸は検証していません。到達できた場合の帰船着陸では風を止めない実装です。

## 減速について分かった範囲

巡航速度パラメーターは8m/sでしたが、APの`trajectory_setpoint`自体が約0.58m/sになっていました。最後30秒の4モーター出力の最大値は0.83626（正規化値）、姿勢目標のpitchは-35.6〜-35.5度です。これらだけではモーター飽和や推力不足を原因と断定できません。

次は、短い海上経路でAPの軌道生成と速度制限がどこで約0.58m/sになるかを切り分けます。根本原因は未確定です。PX4は[軌道を生成](https://github.com/PX4/PX4-Autopilot/blob/381149fb012762f5e38c4a7fdc1b905b28038970/src/modules/flight_mode_manager/tasks/Auto/FlightTaskAuto.cpp)したあと、[位置・速度制御と推力制限](https://github.com/PX4/PX4-Autopilot/blob/381149fb012762f5e38c4a7fdc1b905b28038970/src/modules/mc_pos_control/PositionControl/PositionControl.cpp)を行います。今回の診断はこの区別を保って記録しています。

## 位置と風の切替

機体の新しいGazebo位置を使い、沖合・港内・海沿い・市街地を判定して世界の風を切り替えます。単機用の近似で、異なる場所に同時に異なる風を持つ空間場ではありません。風の[近似力](https://github.com/gazebosim/gz-sim/blob/gz-sim8_8.11.0/src/systems/wind_effects/WindEffects.cc)は従来どおり`質量 × (風速 − リンク速度) × 1/s`。建物の遮蔽・乱流・波・船の動揺・実機の空力校正は含みません。これで「実機が6m/sに対応できない」と結論づけることもできません。

独立した通信・物理試験では、位置マーカーを移し、6→5→4→3→4→5→6m/sの7回の切替と、それぞれが物体に与える力を確認しました。[検証結果](transport/verification.json)。これはマーカーの移動であり、AP飛行・市街地到達・配送の証拠ではありません。配送試験側の風の作用確認は[別の結果](nominal/wind-verification.json)で、`all_zones_exercised=false`です。

開発時の最初の通信試験では、検証器が最初の風を次の切替後まで一定と仮定したことと、同じ時刻の切替前後の位置選択により判定が失敗しました。生ログと失敗判定を残し、区間を分けて検証・再確認しました。その後、持ち運べるCLIでも再実行しています。開発中には位置変更サービスが応答せず5回の切替で終了した試行もあり、生ログを保持しました。通信応答だけで終了せず、観測位置を確認して10秒以内で同じ位置要求を再送する処理を追加しました。さらに最後の風速切替が一度の応答で確認できなかった試行も保持し、同じ風速要求を最大約3.5壁時計秒まで確認・再送して、未確認なら停止する処理を追加しました。配送飛行の失敗をこの修正で成功扱いにはしていません。

## 動画とバッテリー

動画は今回のGazeboの実カメラ記録です。ヘッダーに残量・電圧・シミュレーター時刻を表示します。時刻が合う過去2秒以内の値だけを使い、欠測は`UNAVAILABLE`です。[PX4のバッテリー模擬値](https://github.com/PX4/PX4-Autopilot/blob/381149fb012762f5e38c4a7fdc1b905b28038970/src/modules/simulation/battery_simulator/BatterySimulator.cpp)は経過時間による値で、風や推論の電力、Wh、実機の航続時間を測っていません。

## 再現と確認

依存関係・契約は[エージェント向け文書](../../agents/yokohama-wind-battery.md)を参照してください。DockerのCPUシミュレーターを明示的に起動します。

```sh
python scripts/yokohama_sitl.py --phase flight --approve-sitl \
  --output-dir RUN --decision-backend fixture --wam-profile motion-v4 \
  --sea-round-trip --deliver-payload --wind-profile harbor-nominal \
  --wind-after-takeoff --timeout-seconds 2400
python scripts/smoke_yokohama_wind_profile.py --approve-sitl \
  --flight-config RUN/config.json --output-dir TRANSPORT_RUN
python scripts/export_yokohama_wind_report.py --run RUN --output REPORT
```

ローカル全体テストは3,484 passed / 1 skipped。その後の検証器・表示範囲の修正は関連テスト77 passed / 1 skippedで確認しています。実行CLIの不正な組合せ・未実装の12m/s指定は起動前に拒否されました。[拒否記録](cli-negative.json)。公開用ファイルには元ログ・実行ソース・検証器のハッシュを残します。

原典：横浜市・Project PLATEAU（2024年度公開カタログ）を加工 / CC BY 4.0。[出典・ライセンス](../yokohama-urban-scene/ATTRIBUTION.md)。
