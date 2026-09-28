# 荷物を運び、届けて、船へ帰る

**荷物を載せた船上離陸 → 海上AP → 市街地の実VLA＋WAM → 荷物分離・着地・模擬受領 → AP帰船・着陸が、同じPX4/Gazebo飛行で成立しました。**

[全行程の3D記録](index.html) · [配送の実カメラ動画・等速](cargo-delivery.mp4) · [機上動画・8倍速](onboard-timelapse.mp4) · [結果JSON](mission-result.json)

1機・静止船・無風のシミュレーションです。受領は荷物の接触と静止を確認する**模擬受領ステーション**で、人間の受領や実機配送ではありません。

## 同一飛行で観測したこと

実行 `yokohama-6acc29204d62`、ソース `819b90c13ffc03a5fbd3773aeecb91278e988380`。実測経路 **3529.33 m**、7,232 位置記録、1687.43 simulator seconds。13地点の30秒保持が合格しました。

50 gの箱は別の動的物体です。船から機体に接続して運び、配送パッド上空3 mの承認済み低高度保持でジョイントを解除します。位置の書換えや配送先への生成は使っていません。分離コマンドは2回送信しました（既定上限3回以内の再送）。コマンド送信とは別にジョイント解除、機体との距離、パッド接触、3.344秒の静止を観測しました。パッド中心からの水平距離は **0.013 m**。別のホスト側受領処理が新鮮な受領記録を発行し、その記録を検証してから上昇・帰船を許可しています。最後に甲板接触、着陸、disarmを確認しました。

接触なし、静止未達、古い観測、別の実行の受領記録などは拒否します。指示送信、分離、着地、受領、帰船完了は別々の証拠です。[荷物の検証結果](verification-payload.json) · [受領記録](payload-receipt.json)

## 海上はAP、市街地だけ実モデル

片道1 kmの海上区間にモデル要求は0件です。街区までさらに片道400 mの接続経路があり、海岸境界・船は追加した仮想設定です。実際の海岸線の測量結果ではありません。

市街地D1/D2のAP保持中に、新しいカメラ観測からAeroVLAの提案とANWM予測を生成しました。位置と工程で呼出しを制限し、海上では起動・ウォームアップ・推論をしません。2区間の到達後、モデルを終了して遅延要求を拒否し、残りはAPで実行します。

| 地点 | モデル提案による実測移動 | VLA交換時間 | WAM交換時間 |
| --- | ---: | ---: | ---: |
| D1 | 3.01 m | 16.81 s | 51.73 s |
| D2 | 2.73 m | 16.72 s | 49.05 s |

実VLA呼出し2回、実WAM予測4枚。以前の成功出力の再生ではありません。既存motion-v4アダプターを利用し、今回は追加学習していません。モデルは承認済み短区間のパラメーターを選び、独立した建物形状Rulesと画像整合性を通った提案をAPへ渡します。経路全体は事前設定です。理想的Rulesへの優越や画像の完全一致を合格条件にしていません。

## E2E / Runtime Verification

最初にGPUなしのfixture判断で実PX4/Gazeboの全行程と荷物受領を確認し、その後の最初の実モデル試行は、旧VMを参照する起動設定により推論前に停止しました。[この失敗](retained-failures.json)を保存し、接続先を単一の設定に束縛する修正と課金前の確認を入れてから、実モデル飛行を完了しました。飛行・モデル判断・荷物受領の3検証がすべて合格。runtime境界は新しい観測 → 実モデル → Rules → MAVLink → 実移動 → 低高度保持 → 物理分離 → 接触・静止 → 受領 → 帰船・着陸です。

```sh
python scripts/yokohama_sitl.py --phase flight --approve-sitl --sea-round-trip \
  --deliver-payload --decision-backend native \
  --native-service-config /secure/native-service-config.json --wam-profile motion-v4 \
  --output-dir /tmp/yokohama-cargo-native --timeout-seconds 2500
python scripts/verify_yokohama_sitl.py /tmp/yokohama-cargo-native \
  --output /tmp/yokohama-cargo-native/verification.json
python scripts/verify_yokohama_decisions.py /tmp/yokohama-cargo-native \
  --output /tmp/yokohama-cargo-native/decision-verification.json
python scripts/verify_yokohama_payload.py /tmp/yokohama-cargo-native \
  --output /tmp/yokohama-cargo-native/payload-verification.json
python docs/examples/yokohama-cargo-flight/verify_bundle.py
```

公開検証は保存済み予測画像・移動・荷物の証拠とハッシュを再計算します。推論の再実行ではなく、生ログ全件の検証はローカル保管した原本を使います。元の保持・到達・画像条件を緩めずに検証しました。先行する[荷物なしの飛行](../yokohama-sea-city-flight/REPORT-ja.md)と、その失敗記録も保持しています。

配送工程の追加費用概算 **$1.0612**（推論前の失敗分$0.5486を含む）、累計 **$16.7063 / $17**。市街地でモデル処理を終えた後に所有VM・ディスクを削除し、配送・帰船は同じローカル飛行で続行しました。削除確認済みです。[費用記録](cost.json)は[公式VM料金表](https://cloud.google.com/products/compute/pricing/accelerator-optimized)と保持時間に基づく見積もりで、請求確定額ではありません。

## 残る範囲

今回成立したのは静止環境・1機の配送工程です。梱包の損傷、実機の投下機構、強風、動く船、10機の管制、機上計算と全行程の消費電力は未検証です。受領記録は人間の承認ではありません。方位の変換にはシミュレーターの観測真値を利用しています。WAMの画像ゲートは既知の可視形状の整合性で、一般的な障害物認識や未知領域の安全性を証明しません。

3D表示は実測位置の補間で、機体・荷物マーカーは見やすく拡大しています。配送動画と機上動画は実カメラ記録、WAM予測は別表示です。次は条件を固定した風を加え、配送・帰船の成立範囲を確認できます。

原典：横浜市 / Project PLATEAU、加工データ。[出典・ライセンス](../yokohama-urban-scene/ATTRIBUTION.md) · [保守契約](../../agents/yokohama-payload-delivery.md)

## バッテリー表示の追加

同じ飛行の `battery_status` と元のGazeboフレームを使い、[残量・電圧付き動画](battery/battery-flight.mp4)を追加しました。元の動画・生ログ・予測画像は保持しています。約2秒間隔の観測を8倍速で再生し、画像時刻以前の最新バッテリー値を対応させます。2秒より古い値や欠測は埋めず、12 / 798フレームを `UNAVAILABLE` と表示します。[対応表](battery/battery-frames.json)・[メタデータ](battery/battery-metadata.json)。

PX4 `battery_simulator` はarm中の時間から電圧を生成します。この試験では `SIM_BAT_DRAIN=3600`、`SIM_BAT_MIN_PCT=0`。電流は `-1`（未計測）で、Wh、実際の航続可能時間、風やVLA/WAMの消費電力は分かりません。disarm後の模擬電圧回復もそのまま記録しています。[実行版のPX4ソース](https://github.com/PX4/PX4-Autopilot/blob/381149fb012762f5e38c4a7fdc1b905b28038970/src/modules/simulation/battery_simulator/BatterySimulator.cpp)。

風を加えた別のCPU試験は[別レポート](../yokohama-wind-battery/REPORT-ja.md)に記録します。この実モデル飛行の無風条件は変わりません。
