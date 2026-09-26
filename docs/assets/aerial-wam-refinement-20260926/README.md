# WAM判断待ち時間の改善 — 通信を1回にまとめる試験

Rulesへの勝利を要求せず、事前に決めた到達・安全・待ち時間・動作の条件で評価した。
**新規3場面の到達・着陸・disarmは3/3、既存の運用条件達成は3/3、
観測から判断・dispatchまで120 wall秒以内という追加目標は0/3だった。**
これは既知の静止シーン各1回の工程確認であり、一般的な成功率を示すものではない。

[実測軌跡・高度と観測画像の再生](index.html) · [実際のGazeboカメラ動画](observed-flight.mp4) ·
[全測定値](summary.json) · [固定した条件の原文](protocol.md) · [固定記録](freeze.json)

| 場面 | WAM第一候補 → 実行 | 距離 m | 飛行 sim秒 | 最小機体外余裕 m | 観測→判断 / dispatch wall秒 | 120秒目標 |
|---|---|---:|---:|---:|---:|---|
| gap | `left_detour` → `left_detour` | 34.94 | 41.58 | 1.122 | 128.41 / 128.55 | 未達 |
| climb | `left_detour` → `climb` | 17.53 | 26.46 | 1.100 | 146.68 / 146.78 | 未達 |
| detour | `right_detour` → `right_detour` | 28.26 | 34.74 | 0.812 | 148.82 / 148.96 | 未達 |

経路指標はdispatchから目的地への到達・1 sim秒滞在まで。着陸は別ログで検証した。
建物接触通知は各場面0。機体を包む半径0.6 mを差し引いた記録位置間線分の余裕を併用した。
地面接触の正の対照はあるが、通知0だけで連続時間の無接触を保証しない。

[入力とLPIPS採点の診断](DIAGNOSTICS.md)：採点法の交換だけでは隙間通過・上昇選択は改善しなかった。

元の相対リンクを保つため、固定条件のコピーは編集していない。[リポジトリ上の原文](https://github.com/pome223/missionos/blob/a50cccda6fc4f5d83f6d0eac0622716f89bfc423/docs/agents/aerial-wam-refinement-pilot-20260926.md)では文中のリンクも参照できる。

## モデルの選択とRulesを分ける

WAM第一候補のまま制約を通ったのは2/3。
要求動作を実行したのは2/3、そのうちWAM第一候補のまま実行したのは1/3。
ビル間での迂回到達を隙間通過に数えず、Rulesが選択肢を1本に絞った上昇をWAMの上昇判断に数えない。
これらはモデルを不採用にするためのRules比較ではなく、モデルと安全制約が担った部分を区別する記録である。

生成画像は観測カメラ映像とは別枠で表示した。画像と目標画像のMSEは通過可能性・衝突確率として検証していない。
今回もGateway停止・再観測機能はdepth方式のままで、ANWMは別の実験用ランナーを通った。
VLA、Jev、事後学習、実機飛行は行っていない。

## 変えたものと変えなかったもの

転送方式は`single_ssh_tar_v1`。入力と出力を単一SSHセッションにまとめ、毎回のモデルプロセス起動は維持した。
公開モデル・上流コード・VAEのrevision、seed 42、250 diffusion steps、16枚の実RGB-D/姿勢、5 m先の候補姿勢、MSE順位は同一。
候補経路と目的地画像は手作業で用意した。安全ゲート、hover確認、180 wall秒の実行期限も変えていない。
120秒はそれより厳しい工程改善目標である。

全候補の推論完了後に行った読み取り診断では、約18.15 GBのcheckpoint SHA256確認が52.31秒だった。追加モデル呼び出しは0。残差すべてを通信時間とは呼ばない。

初回能力試験、単一SSH試験、常駐試験は別々のコホートとして保存する。
観測入力の異なる各1試行から厳密な速度比やモデル能力の改善率を推定しない。
前の遅い結果を置き換えず、短い待ち時間を隙間通過・上昇判断の改善に読み替えない。

## 条件・費用

全3試行、8候補の実ANWM推論。失敗や不利なモデル結果の差し替えはない。
到達範囲0.3 m・1 sim秒滞在、着陸/disarm、接触通知0、機体外余裕0.25 m以上、
経路60 m・120 sim秒以内、観測年齢180 wall秒以内を維持した。
未知の街区、移動障害物、任意経路生成、時刻に整合した予測は未検証。

今回のL4確保から削除確認まで28.8分、
保守的費用見積もり$0.78。
累計は**$6.45**の見積もりで、ユーザー指定$10以内。
1時間$1.10とディスク・通信$0.25の余裕を使用し、請求明細とは未照合。
45分のVM自動削除と起動ディスク自動削除を設定し、終了後に両方の不存在を確認した。
3個の試験用シミュレーターも削除済み。クラウド転送は公開ランタイムと新規の画像・姿勢入力だけ。

## E2E / Runtime Verification

実行コード: `a50cccda6fc4f5d83f6d0eac0622716f89bfc423`。プロトコルSHA256: `952546420a3a18f1e24f0c07f9c9d82f245f4711e0a85210a54ce50f898d6972`。
`freeze.json`のソースhashと実行時ソースが一致することを確認してから開始した。

各場面`gap`、`climb`、`detour`を新しい出力先・シミュレーターで実行した。
`GPU_CONFIG`は今回の転送方式を明示するローカル設定、`ASSETS`は固定済みアセット、
`RETAINED_INSTRUCTION_REF`は保持された許可への参照。設定・鍵・承認記録は公開bundleに含めない。

```sh
RUN_PX4_URBAN_WAM_TRIAL=1 python scripts/px4_urban_wam_trial.py \
  --phase run --scene "$SCENE" --selector anwm \
  --assets-dir "$ASSETS" --gpu-config "$GPU_CONFIG" --output-dir "$RUN" \
  --approved-instruction-ref "$RETAINED_INSTRUCTION_REF"
python scripts/verify_urban_wam_trial.py --root "$RUN" --output "$RUN/verification.json"
python scripts/audit_urban_wam_candidates.py --input-dir "$RUN/input" \
  --result "$RUN/forecast/result.json" --output "$AUDIT"
```

境界は新規観測→L4上の実ANWM→入力と出力のhash検証→モデル順位→独立Rules→署名付きdispatch→
PX4/Gazeboの実測移動→到達滞在・着陸/disarm→生ログverifier。3件の元記録を再計算して確認した。
契約テストと実プロセスのfixture smokeは実ANWM/実飛行の証拠と区別する。

```sh
python -m pytest -q tests/contract/test_anwm_resident.py \
  tests/contract/test_urban_wam_transport.py tests/contract/test_urban_wam_candidate_audit.py \
  tests/contract/test_urban_navigation.py tests/contract/test_urban_anwm_preview.py \
  tests/contract/test_aerial_anwm_runtime.py
python docs/assets/aerial-wam-refinement-20260926/verify_report.py
```

現行実装の対象59テストと全体3298テストが通った。portable verifierはhash、組み込みデータ、
分母、絶対条件、120秒の目標、動作帰属、再生時刻、後片付け記録を確認する。
公開bundleの確認だけで元の物理シミュレーションやモデル推論を再実行したとは主張しない。
再生は4倍のsim時間の実カメラ動画、推論待ちと着陸は省略。HTMLの静止画像は各場面4枚から近い時刻を表示する。

Apartment assets: [OSRF gazebo_models](https://github.com/osrf/gazebo_models), CC BY 3.0, Nathan Koenig / Cole Biesemeyer.

UI確認: ローカルHTTPで3場面の切替、上昇の途中時刻と高度、終点での再生停止、実MP4の再生進行と停止、360pxでの文書幅、JavaScriptエラー0を確認した。直接file表示は確認していない。
