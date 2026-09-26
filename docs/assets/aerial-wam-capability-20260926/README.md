# WAMの絶対的な航法能力を試す — 2026-09-26

Rulesに勝つことを条件にせず、到達、安全、待ち時間、要求した動作を評価した。
**新規3場面・各1試行で、ANWM予測を使う経路選択からPX4/Gazeboの到達・着陸・disarmを3/3確認した。**
事前の運用上限は3/3で満たした。
WAM第一候補が制約を通ったのは2/3、要求動作を実行したのは2/3、
要求動作をWAM第一候補のまま実行したのは1/3だった。
これは既知の静止シーンにおける小規模な能力確認であり、一般的な成功率の推定ではない。

[観測軌跡・高度・カメラ画像の再生](index.html) · [実際のGazeboカメラ動画](observed-flight.mp4) ·
[全測定値](summary.json) · [実行前に固定した条件](protocol.md) · [固定記録](freeze.json)

| 場面 | WAM第一候補 → 実行 | Rulesによる変更 | 飛行距離 | 飛行時間（sim） | 最小機体外余裕 | 観測→dispatch（wall） | 要求動作 |
|---|---|---|---:|---:|---:|---:|---|
| ビル間 | `left_detour` → `left_detour` | なし | 35.05 m | 41.62 s | 1.107 m | 142.85 s | 未確認 |
| 上昇 | `left_detour` → `climb` | あり | 17.68 m | 26.92 s | 1.117 m | 157.60 s | 確認 |
| 側方回避 | `right_detour` → `right_detour` | なし | 28.19 m | 34.70 s | 0.855 m | 165.54 s | 確認 |

距離・飛行時間はdispatchから目的地の到達・1秒滞在確認まで。着陸区間は含めない。
余裕は観測位置間の線分と建物の境界から、機体を包む半径0.6 mを差し引いて算出した。
建物接触通知は全場面0。地面接触の正の対照は得られたが、建物接触ストリーム自体に発火はなく、
通知0だけで連続時間の無接触を保証しない。記録軌跡の幾何検証を併用した。

## 何ができ、何が残ったか

- **到達の実行接続は確認できた。** 実際のRGB-D・姿勢を入力し、8候補分の実モデル推論を実施。
  生成結果を経路選択に使い、独立した制約を通過した経路をPX4が飛んだ。
- **ビルの隙間を通る選択は未確認。** ビル間シーンではWAMが左迂回を選んだ。
  距離35.05 mは事前の60 m上限内であるが、到達と隙間通過の能力は別に扱う。
- **上昇はRulesの制約によるもの。** WAMの左迂回案は制約を通らず、唯一許可された上昇を実行した。
  上昇動作の実行は確認できるが、WAMによる上昇選択には数えない。
- **待ち時間が大きい。** 最後の観測からdispatchまで142.8〜165.5 wall秒。
  180秒以内の静止ホバリング試験には収まったが、動く障害物への即時回避能力は示していない。

幾何・depth経路との比較試行、最短経路との勝敗、優越性による中止判定は行っていない。
今回の到達はRulesを含むシステムの成果、モデルの提案能力は上表の第一候補として別に記録した。
WAMを省略した試験ではない。一方、Gatewayの停止・再観測アダプターは引き続きdepth方式であり、
今回の実験用ANWM接続とは別経路。VLA・Jev・事後学習・実機飛行は実施していない。

右迂回の生成画像には、観測されたGazeboの低層建物と一致しない高層市街地も描かれた。
生成画像の見た目を通過可能性の証明には使えない。予測の忠実性は別の検証課題として残る。

## 条件・手間・費用

- 既知の3場面、同じ固定条件で各1回。飛行再試行0。失敗結果の置き換えやモデル出力後の条件変更なし。
- 公開ANWMの固定checkpoint、seed 42、250 diffusion steps、16枚・4 Hzの実シミュレーターRGB-D履歴。
  モデルと上流コード・VAEのrevisionは測定値内に保存。
- 人が用意した経路候補、目的地画像、カメラ校正、建物シーンを使用。
  5 m先の候補姿勢に条件づけた生成画像と目的地画像のMSEで順位を付けた。
  任意の経路生成、未知の街区、時刻に整合した未来予測、衝突確率は未検証。
- 1台のL4を使用。GPU確保前に共有quota待ちと1件の在庫不足があった。どちらも飛行試行に含めず、
  元の記録を保持した。実VMは75分の自動削除上限で作成し、終了後にVM・起動ディスクの不存在を確認した。
- 実VMの確保から削除確認までの上限は30.3分。
  今回の成功した確保分は保守的見積もり$0.80、
  過去の$4.61と在庫不足時の保守的留保を含む累計は**$5.67以下の見積もり**。
  請求明細とは未照合。ユーザー指定の累計$10以内。
  見積もりは1時間$1.10とディスク・通信$0.25の余裕を含む。
- シミュレーター3個も削除確認済み。クラウドに転送したのは公開済みランタイムと新規センサー入力だけ。

## E2E / Runtime Verification

Runtime revision: `de6f0dd4ac8833339946fd0c3d6d690394308fc6`。
事前登録commit `8eeb09b`は文書のみ。実行時の対象runtimeソースが固定時のhashと同一であることを確認した。

次のコマンドを、`SCENE=gap`、`climb`、`detour`の順で、新しい出力先・別々のシミュレーターに対して実行した。
`ASSETS`は検証済み固定アセット、`GPU_CONFIG`は試験専用VMのローカル接続設定、
`RETAINED_INSTRUCTION_REF`は保持されたユーザー許可への参照。設定・鍵・承認記録はこの公開bundleに含めない。

```sh
RUN_PX4_URBAN_WAM_TRIAL=1 python scripts/px4_urban_wam_trial.py \
  --phase run --scene "$SCENE" --selector anwm \
  --assets-dir "$ASSETS" --gpu-config "$GPU_CONFIG" --output-dir "$RUN" \
  --approved-instruction-ref "$RETAINED_INSTRUCTION_REF"
python scripts/verify_urban_wam_trial.py --root "$RUN" \
  --output "$RUN/verification.json"
```

境界: native RGB-D/pose → 実ANWM推論 → hash検証 → モデル順位 → 独立Rules →
署名付きdispatch → PX4/Gazeboの実測軌跡・到達・着陸/disarm → 生ログ再検証。
3試行すべてprocess exit 0、生ログverifier合格。推論receiptもfixtureでないことを確認した。
hover許容位置0.25 m・姿勢0.1 radとtelemetry生存確認は実行側・元verifierで検証した。

```sh
python -m pytest -q tests/contract/test_urban_navigation.py \
  tests/contract/test_urban_anwm_preview.py tests/contract/test_aerial_anwm_runtime.py
python docs/assets/aerial-wam-capability-20260926/verify_report.py
```

契約チェックは37 passed。portable verifierはhash、組み込みデータ一致、全試行の分母、
絶対条件と動作帰属、時刻順、後片付け記録を確認する。生ログがない公開bundleだけで物理実験を再現したとは主張しない。
動画は実際のカメラ画像を4倍のsim時間で再生し、推論待ちと着陸を省略している。
再生HTMLは観測位置と各場面4枚のカメラ画像を表示し、生成画像を別枠に置く。

表示確認: loopback HTTPで3場面の切替、時刻0・途中・終端、上昇の高度表示、
再生の終端停止、実観測MP4の再生・一時停止、360 px幅の横溢れなし、JS errorなしを確認した。
動画は960×680、25.875秒。直接file URLでのブラウザー動作は別途確認していない。

次に確認すべき点は、画像の見た目の類似度による順位付けが要求動作に結びつくか、
およびホバリング待ち時間を実用上許容できる範囲に短縮できるかである。
事後学習が必要かはこの試験だけでは判断しない。Rulesに勝てなかったことを理由に研究を終了しない。

Apartment assets: [OSRF gazebo_models](https://github.com/osrf/gazebo_models),
CC BY 3.0, Nathan Koenig / Cole Biesemeyer.
