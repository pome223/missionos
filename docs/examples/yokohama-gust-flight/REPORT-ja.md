# 海上APの通過点修正と、シード固定の突風

海上の細かい通過点を直線区間の終点指定に変更し、以前未達だった沖合1 kmから岸への移動と30秒保持を確認しました。ランダム突風も実装し、CPUのPX4/Gazebo配送ミッションで試しました。**全行程の配送・帰船は今回も未達です。**

| 試験 | 海上区間 | AP保持 | 最終工程・結果 |
|---|---|---|---|
| 旧・定常風 | 岸の718.018 m手前で時間切れ | 1/13 | 海上。前回の失敗を保持 |
| 修正・定常風 | 岸に到達、30秒保持合格 | 4/13 | 市街地D2。模擬WAMの画像整合性ゲートで停止 |
| 修正・突風 seed 20260928 | 岸の保持合格 | 3/13 | 00-D1 / ValueError: City decision hold, reserve, heading or estimator continuity lost |

各飛行条件は1回ずつ。繰返し成功率や実機の耐風性能を示す試験ではありません。

[軌跡と残量・風表示付き動画](index.html) · [定常風MP4](nominal-direct/video/battery-flight.mp4) · [突風MP4](gust-flight/video/battery-flight.mp4) · [全結果](summary.json) · [前回の未達レポート](../yokohama-harbor-wind/REPORT-ja.md)

## 海上で遅くなった原因と変更

旧ログでは2番目の通過点で `seq_current=1` が止まり、機体がその先を飛んでも目標更新がありませんでした。通過点への観測上の最接近は0.625 m、通過判定は0.5 mです。この距離は離散サンプルの値であり、連続時間の最小距離ではありません。PX4は古い通過点を保ったまま、約0.58 m/sの軌道速度目標を出し続けていました。

海上の1 km直線を50点に分割するのをやめ、終点1点＋その場のLoiterに変更しました。風6/5/4/3 m/s、力の係数1/s、経路形状、速度指定8 m/s、到達時間上限435壁時計秒、到達・保持・配送基準は維持しています。市街地経路は変更していません。

共通のシミュレータ時刻150–240秒（どちらも沖合6 m/s）の中央値は、実速度が **0.583 → 2.931 m/s**、AP軌道速度目標が **0.582 → 8.000 m/s** でした。岸での保持は30.156秒、最大水平誤差0.153 m、最大鉛直誤差0.479 m。

これにより、この経路構成で発生した通過点停滞は回避できました。PX4内部の全分岐を直接計測した根本原因証明や、8 m/s実速度の達成ではありません。強風による速度追従の低下は残ります。[診断値と元ログのハッシュ](ap-analysis.json)。照合した実装は [PX4 PositionSmoothing](https://github.com/PX4/PX4-Autopilot/blob/381149fb012762f5e38c4a7fdc1b905b28038970/src/lib/motion_planning/PositionSmoothing.cpp) と [FlightTaskAuto](https://github.com/PX4/PX4-Autopilot/blob/381149fb012762f5e38c4a7fdc1b905b28038970/src/modules/flight_mode_manager/tasks/Auto/FlightTaskAuto.cpp) です。

## 突風の条件と、実際に確認した範囲

発生時刻・継続時間・強さをseed 20260928で事前生成。離陸後の初回定常風開始から20–35秒後に最初の突風、6–10秒継続、以後45–75秒の定常風区間を挟みます。3600シミュレータ秒の予定を保存し、失敗しても引き直しません。

| 地域 | 定常風 | 突風の設定範囲 |
|---|---:|---:|
| 沖合 | 6 | 7–8 m/s |
| 港内 | 5 | 6–7 m/s |
| 海沿い | 4 | 5–6 m/s |
| 市街地 | 3 | 8–12 m/s |

すべて東向き。突風値は増分でなく総風速です。Gazeboの1秒時定数で変化します。ユーザー提示の想定を使った人工的な試験条件で、実際の横浜の気象・建物の乱流・校正された空力モデルではありません。

岸での突風あり保持は30.388秒。保持中に港内5 m/sから6.802 m/sへの突風が入り、最大水平ずれ0.919 m、最大速度0.421 m/sで既存基準を満たしました。これは1回の保持結果です。

今回の飛行で記録した突風IDは **[0, 1, 2, 3, 4, 5, 6, 7, 8]**、適用地域は **['offshore', 'harbor', 'coast', 'city']**。設定ピークは **[7.689, 7.947, 7.914, 6.512, 6.67, 6.802, 5.058, 5.025, 8.1, 9.153] m/s**。予定の生成、Gazeboの受理、物理作用、飛行完遂は別々に判定しています。[受理記録](gust-flight/wind-transitions.json) · [風の作用検証](gust-flight/wind-verification.json)。未訪問地域や未発生ピークの飛行性能は未検証です。

別の最小Gazebo試験は、マーカーを移動して全4地域と突風の組合せを確認しました。推進力のない物体と風を受けない対照物体で、切替後の連続した応答を検証しています。**この試験ではPX4もドローン飛行も使っていません。** [検証結果](transport/verification.json)。開発時に同時刻の境界切替を拒否した検証不具合を修正し、元の不合格と修正後の再実行を両方保存しました。[全試行](development-attempts.json)

市街地入口では最後の突風を9.153 m/sとして受理した直後、速度0.364 m/sが判断時の上限0.3 m/sを超え、移動を拒否しました。ずれは0.083 m、残量は約83%で、今回は速度条件が停止理由です。[拒否の照合](city-gust-rejection.json)。

動画の最終カメラ時刻は626.252秒で、最後の突風ID 8の送信開始627.252秒より前です。動画に映る突風はID 0–7までで、最後の開始は軌跡・受理記録で確認できます。

最後の突風ID 8は受理後すぐ試験が終了したため、物理作用の所定の観測窓と回復がありません。**全期間の風検証は不合格**として残しています。それ以前の完了したID 0–7だけを再計算した[区間限定の風検証](completed-gust-prefix.json)は合格です。両者を混同しません。飛行側の停止応答待ちも速度ゲートで中断しましたが、ホスト側のfinallyで模擬モデル停止・セッション取消しを確認し、コンテナを削除しました。

## 市街地で止まった理由と限界

定常風試験は市街地の判断更新を1回反映し、D2で2回目を拒否しました。CPU模擬WAM画像の既知画素率はhold 0.561 / 移動 0.559。画像の一致度だけでなく可視構造の量を求める既存ゲートに達せず、次の区間は送信していません。[拒否記録](nominal-fixture-rejection.json)。この結果を実WAMの推論失敗とは呼びません。ゲートは緩めていません。

今回の市街地判断はCPU fixtureです。実VLA/WAM、新しいGPU推論、機上電力計測、10機同時飛行、移動船は未実施。全行程Verifierは未完了のworker記録で必要なholdsがなく停止し、配送Verifierも配送記録がないため実行を完了できません。いずれも合格として数えていません。風の作用と各保持は保存された別記録で照合しています。[定常風の保持再計算](nominal-direct/hold-audit.json) / [突風の保持再計算](gust-flight/hold-audit.json)。

動画は記録済みGazebo RGBにヘッダーを追加。バッテリーはPX4の時間ベースの模擬値で、電流・Wh・風や推論による消費は測定していません。風表示は画像時刻までに受理された設定値で、局所風の実測ではありません。映像と軌跡の再生時刻は独立です。

今回の追加GPU料金は $0。従来の累計見積 $16.7063 / 上限 $17 は変わりません。 所有したシミュレータコンテナは全て削除しました。

## 再実行と検証

```sh
python scripts/yokohama_sitl.py --phase flight --approve-sitl --output-dir RUN \
  --decision-backend fixture --wam-profile motion-v4 --sea-round-trip \
  --deliver-payload --wind-profile harbor-nominal --wind-after-takeoff \
  --gust-seed 20260928 --timeout-seconds 2400
python scripts/smoke_yokohama_wind_profile.py --approve-sitl \
  --flight-config RUN/config.json --output-dir TRANSPORT --gust-seed 20260928
python scripts/verify_yokohama_sitl.py RUN --output RUN/verification.json
python scripts/verify_yokohama_decisions.py RUN --output RUN/decision-verification.json
python scripts/verify_yokohama_payload.py RUN --output RUN/payload-verification.json
python scripts/export_yokohama_wind_report.py --run RUN --output REPORT
```

定常風比較は `--gust-seed` のみ省略。各RUNは新規ディレクトリ。環境はPX4 `381149fb012762f5e38c4a7fdc1b905b28038970` / Gazebo 8.11、CPU、50 g荷物、静止船。実行したコマンドの出力・エラーと元データは保持しています。全テスト3501 passed / 1 skipped、Ruff・差分検査合格。予定は [protocol.json](protocol.json)、実行識別子とコード・原記録ハッシュは各 [定常風結果](nominal-direct/summary.json) / [突風結果](gust-flight/summary.json) に収録。

次は、強風下のカメラ姿勢と市街地の可視範囲を分離して確認し、現在の拒否ゲートを保ったまま再観測・再保持で配送工程を続けられるか調べます。
