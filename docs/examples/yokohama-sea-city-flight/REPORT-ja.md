# 船から市街地へ、実VLA＋WAMを使って帰船

**静止船から離陸し、海上はAP、市街地では実VLA＋WAMを2回使い、配送地点を経由して船へ着陸・disarmする一本のシミュレーションが通りました。** 荷物の投下・受領はまだ含みません。

[全行程の3D記録と予測画像](index.html) · [実カメラ動画・8倍速](onboard-timelapse.mp4) · [結果JSON](mission-result.json)

## 観測された結果

同一機体・同一実行 `yokohama-f17245dce007`。記録位置 6,989 点、実測経路 3501.39 m、記録時間 1577.46 simulator seconds。11地点の30秒保持、帰船・着陸・disarm、着陸時の甲板接触を確認しました。ソース `51d6de5962fb1f70cc53e85bfb06fd8a85449042`。

往路と復路の海上区間はそれぞれ1,000 mです。船とD1の間には、さらに片道400 mの接続経路があります。海岸境界は元の街区モデルの外に設けた**シミュレーション上の境界**で、実際の海岸線を測量したものではありません。船・海面も追加した静止形状です。

| 判断地点 | モデル提案による実測移動 | VLA交換時間 | WAM交換時間 |
| --- | ---: | ---: | ---: |
| D1 | 2.47 m | 17.00 s | 51.98 s |
| D2 | 3.01 m | 16.64 s | 49.75 s |

実観測へのVLA呼出し2回、追加学習済みANWMによる予測4枚を、その場で生成しています。以前の市街地飛行の出力を再生したものではありません。WAMには先行研究のmotion-v4アダプターを使用し、今回の追加学習はありません。

## 海上AP・市街地モデルの切替

船上と海上ではモデルを起動せず、D1のAP保持を確認してから起動・ウォームアップします。D1とD2で新しい観測を取り、VLAの短区間提案、WAMの可視形状整合性、独立した建物形状Rulesを確認してAPへ渡します。提案・許可・送信・到達を別々に記録しました。

D2のモデル区間の到達後にモデル処理を終了し、遅れて届く要求を拒否しました。残りの配送地点への移動と帰路はAPが実行します。海上のモデル要求は0件で、復路へ入る前の終了も観測しました。位置だけ、工程名だけではモデル呼び出しを許可しません。

モデルは約1〜3 mの承認済み移動区間のパラメーターを選びます。経路全体の生成や自由な行動選択を検証したものではありません。元の画像整合性・Rules・到達条件は緩めていません。画像の完全一致や理想的Rulesへの優越も合格条件にしていません。

## 確認範囲

1機、無風、静止建物、静止船のPX4/Gazeboシミュレーションです。配送地点は上空の到達・保持地点です。荷物の投下と受領、動く甲板、強風、10機の発着調整、実機、電池消費の改善は未検証です。方位の世界座標とAP推定座標の変換にはGazeboの観測真値を使っています。

表示する予測と移動後画像は別の記録です。到達直前の実画像は確認用で、厳密な未来時刻の同期比較や未観測空間の安全性の証明ではありません。過去の[市街地単独の成功と失敗](../yokohama-integrated-flight/REPORT-ja.md)は、そのまま保持しています。

## E2E / Runtime Verification

最初のCPU試行は、沖合へ移した出発点と市街地の高度基準の差により、2区間目で約19 cmの高度誤差が残って停止しました。[失敗記録](retained-failures.json)を保存しています。観測したAP相対高度と世界高度を対応づけ、15 cmの到達条件を変えずに修正しました。GPUなしで修正後の全行程を検証してから実モデルへ接続しています。次のコマンドの境界は、海上AP → 新しい市街地カメラ観測 → 実モデル → Rules → MAVLink → 実到達 → 配送地点 → 海上AP → 帰船・着陸です。

```sh
python scripts/yokohama_sitl.py --phase flight --approve-sitl --sea-round-trip \
  --decision-backend native --native-service-config /secure/native-service-config.json \
  --wam-profile motion-v4 --output-dir /tmp/yokohama-sea-city-native --timeout-seconds 2500
python scripts/verify_yokohama_sitl.py /tmp/yokohama-sea-city-native \
  --output /tmp/yokohama-sea-city-native/verification.json
python scripts/verify_yokohama_decisions.py /tmp/yokohama-sea-city-native \
  --output /tmp/yokohama-sea-city-native/decision-verification.json
python docs/examples/yokohama-sea-city-flight/verify_bundle.py
```

公開bundleの検証は保存済み記録の再計算であり、GPU推論の再実行ではありません。生のログと重みは非公開の実験保管先に保持しています。

今回の概算費用 **$0.9349**、過去分を含む累計 **$15.6450 / $17**。VMとディスクの削除を確認済み。[公式VM料金表](https://cloud.google.com/products/compute/pricing/accelerator-optimized)と保持時間に基づく見積もりで、請求書による確定額ではありません。[費用記録](cost.json)

次は、配送地点への到達を荷物の分離・着地・受領確認につなぎます。

原典：横浜市 / Project PLATEAU、加工データ。[出典・ライセンス](../yokohama-urban-scene/ATTRIBUTION.md)
