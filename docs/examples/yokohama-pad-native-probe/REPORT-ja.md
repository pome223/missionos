# 荷下ろし待ち：実VLA＋WAMの画像診断

**今回の条件では、実モデルを飛行へ接続する条件を満たしませんでした。** 近方の3条件のうち、VLAの提案が想定と一致したのは **1/3** でした。これは固定カメラで取得した画像による診断です。この場面での実VLA＋WAMによる配送飛行は実施していません。

[画像を並べて見る](index.html) · [前段のCPU配送・帰船](../yokohama-pad-queue/REPORT-ja.md)

## 実測

| 観測 | VLAの実出力 | 提案 | この場面で必要な提案 | 判定 |
|---|---|---|---|---|
| 90m・占有中 | 55 49 49 | 短い前進 | 待機 | 不一致 |
| 20m・占有中 | 58 49 49 | 短い前進 | 待機 | 不一致 |
| 20m・退出後 | 55 49 49 | 短い前進 | 短い前進 | 一致 |
| 20m・再占有 | 58 49 49 | 短い前進 | 待機 | 不一致 |

実AeroVLAを4回、実ANWMを8予測分実行しました。モデル重みは変更していません。最初から待機を含む同じ出力範囲を全画像に与え、返された数値を後から停止・前進へ書き換えていません。機体や先行機の将来位置、占有の正解ラベル、退出時刻の台本は推論入力に含めません。

前回の実飛行では待機点がパッドから約90m離れ、先行機が小さく映っていました。追加の20m地点では先行機とパッドの見た目を確認できました。ここはGazeboの固定カメラを配置した診断位置であり、APで到達・保持した実績ではありません。接続経路の建物距離は幾何で事前確認したものです。退出・再占有は台本によるモデル移動で、2機目のPX4飛行ではありません。

近方3件は占有・空き・再占有の工程確認です。再占有は同じ位置へ戻した場面なので、独立した3種類の未知環境での汎化評価ではありません。遠方1件は画像の見え方を調べる補助ケースです。

## WAMの範囲

予測は保持と、変更していないVLA提案の視点です。VLAが保持を選んだ場合、2候補の動作は同一になります。画面左は推論前の実観測であり、予測後に実際に飛んで撮った画像ではありません。数値評価の参照は、最後の実測RGBDから別処理で投影した見えている構造です。一般の障害物認識、衝突予測、パッドが空くまでの時間を検証した結果ではありません。

WAMの時間インデックスは既存motion-v4の1、seedは42、250 diffusion stepsです。これを現実の1秒後と対応づける校正は未検証です。先行機が動き続けている最中の将来位置は今回の入力では評価していません。予測画像が整っているだけでは進入許可に使いません。

## 接続を止めた理由と次の修正

VLAは近方の占有中・退出後・再占有で、すべて前進を提案しました。占有中はbin 58、退出後はbin 55で、画像に応じて値は変わっていますが、「待機」という必要な行動には切り替わっていません。これだけで画像を認識できないとは断定できません。今回確認したのは、固定した指示と出力範囲では、工程に合う提案を生成できなかったことです。Rulesが進入を禁止できることと、VLA自身が待機を提案できることは分けています。

WAMには別の入力条件の問題があります。近方の画像は空が多く、深度で照合できる割合が約45%です。参照画像自身を「完全な予測」として比較しても、既存の60%基準に届きません。この不合格をWAM精度の悪さとは解釈しません。この点の確認はGPU開始後になりました。次回の有料実験より前に検出できるよう、CPUだけの参照自己照合スクリプトを追加しました。[事前判定](input-screen.json)

追加学習を行うなら、先にVLAの待機／前進の切替を対象にします。同じ画像を暗記させる再学習は行わず、別の観測位置・先行機の姿勢・退出途中・再進入を含む学習用データと、開始時刻や位置を分けた評価用データを用意します。現在の4条件は失敗を発見した既知データとして残します。追加学習は今回実施していません。

WAMは学習に入る前に、パッドを十分に観測できるカメラの向きと、実測カメラ姿勢を使う入力契約を整えます。全画面の既存閾値を下げて今回を合格にはしません。その後、実際の新しい観測と対応づけて評価し、最終的には「安全に待つ→配送→受領→帰船」を同一飛行で確認する必要があります。

表示する推論秒数は生成処理内の壁時計です。モデル起動とCPU/GPU間の転送、ファイル転送、AP待機を含む往復遅延ではありません。過去の飛行時の約17秒と直接比較して高速化したとは扱いません。

## 検証と費用

入力画像・16フレームのRGBD履歴・VLA生出力・WAM候補・生成画像のハッシュを再検証しました。[全判定](evaluation.json)・[固定条件](protocol.json)。モデルの提案、Rulesの進入可否、実行、受領を分けています。今回の診断には実行と受領の記録がありません。

追加費用の保守的な見積りは **$0.665**、累計 **$17.371 / $20**。VMと付随ディスクの削除を確認しました。請求確定額ではありません。[費用記録](cost.json)

## 再現

```sh
python scripts/capture_yokohama_pad_views.py --approve-sitl --source-run PASSED_PAD_RUN --output-dir NEW_CAPTURE
python scripts/probe_yokohama_pad_models.py prepare --inputs NEW_CAPTURE --output NEW_INPUTS
# Reviewed, pinned weights and separate native VLA / WAM dependency environments are required.
python scripts/probe_yokohama_pad_models.py vla --inputs NEW_INPUTS --output NEW_VLA --base OPENVLA --adapter AEROVLA
python scripts/probe_yokohama_pad_models.py wam --inputs NEW_INPUTS --output NEW_WAM --vla-results NEW_VLA --upstream ANWM_SOURCE --checkpoint ANWM_CHECKPOINT --adapter MOTION_ADAPTER
python scripts/evaluate_yokohama_pad_models.py --inputs docs/examples/yokohama-pad-native-probe/inputs --results docs/examples/yokohama-pad-native-probe/results --output NEW_EVALUATION.json
```

原典：[横浜市・Project PLATEAUと加工条件](../yokohama-urban-scene/ATTRIBUTION.md)。カメラ画角・色・先行機の形状はこの合成場面の条件です。
