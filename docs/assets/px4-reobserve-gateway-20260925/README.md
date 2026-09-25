# 通常のGatewayから停止・再観測・迂回を実行

`prepare-px4-depth --scene gap --reobserve` で作った同じタスクを、既存の
`execute-sitl` と `job-status` で最後まで扱えるようにした。新しく起動した
認証付きGatewayからPX4/Gazeboを1回飛行させ、停止地点で深度を取り直し、
直進から左迂回へ変更して目的地到達・着陸・disarmを確認した。元ログの独立
検証後にタスクが `completed` になり、承認の再利用は拒否された。

**[実カメラ映像と観測軌跡の再生](replay/index.html)** ·
[測定値](replay/summary.json) · [Gatewayの照合結果](replay/gateway-evidence.json) ·
[元記録と公開ファイルのハッシュ](replay/manifest.json)

| 観測項目 | 今回のGateway経由の1飛行 |
|---|---:|
| 目的地到達、着陸・disarm | すべて確認 |
| 選択の変化 | `forward` → `left_detour` |
| 経路変更 | 1回 |
| 出発から着陸・disarmまでの距離 | 42.9476 m |
| 同区間の経過 | 63.804 sim s |
| 停止から再判断まで | 4.388 sim s / 21.949707 wall s |
| 再判断時の最新画像の古さ | 1.438814 wall s |
| 観測された機体包絡の最小余裕 | 1.066054 m |
| 障害物の接触通知 | 0件 |
| 承認の消費 | 同じタスクの承認1件を1回消費 |
| 同じ承認による再実行 | HTTP 409で拒否 |
| 専用コンテナ | 削除を確認 |
| WAM / Jev / GPU / 学習 | すべて0回 |

新規の統合飛行は **1回中1回到達**。以前の[開発比較](../px4-stop-reobserve-20260925/README.md)
の3飛行（失敗1回と修正後の比較2回）とは別の記録であり、新しい性能比較の
母数に混ぜていない。再観測の待ち時間と最新画像の古さは異なる指標である。

## 承認と観測を同じタスクへ残す

承認範囲は指定されたチェックポイントへの接近、停止後の新規観測、供給済み
候補からの再選択1回、着陸まで。従来の単一路線の承認はこの処理を許可しない。
準備・承認・実行・状態取得は既存のCLI/Gatewayを使う。

タスクには11段階の記録が残った。接触センサーの確認、初回観測、初回選択、
停止観測、再観測、再選択、再開権限の消費、安全判定後の実行、着陸観測、
コンテナ削除、独立検証である。選択結果を実行結果として扱わず、元の画像・姿勢・
軌跡・署名付き指令・着陸記録を再検証するまで到達完了にしていない。

安全ゲートによる中止は `failed` / `safe_aborted` として到達から区別する。
今回のGateway飛行では中止条件は発生していない。この状態対応、承認の流用・
期限切れ・経路やコードの変更、途中失敗、ACKだけの完了拒否は契約テストで確認した。

## 検証した範囲

- 全体テスト: `python -m pytest -q` → **3276 passed**。最初はsandboxがテスト用
  ローカルソケットを拒否したため、通信を許可して同じコードで再実行した。
- 新しい実GatewayとCLI: 認証、準備、明示承認、実行opt-inの拒否、入力制約を確認。
  既存 `gap` / `climb` / `detour` の飛行なしHTTP確認も通過。
- 新しい実Gateway → PX4/Gazebo → 同じTaskStore → CLIの結果取得を1飛行で確認。
- 元のRGB-D再採点、観測時刻・停止・姿勢・経路・終端・cleanupの独立検証が成功。
- 保存HTMLをlocalhostで開き、再生・停止、0秒と40秒への移動、映像の実シーク位置、
  360px幅で横はみ出しがないこと、JavaScriptエラーがないことを確認した。
- 公開ファイル検査はハッシュ・数値・時刻順の検査であり、非公開の元ログを使う
  独立検証とは別。公開資料へ承認ID・署名鍵・DB・私有パスは含めていない。

固定の停止点と候補経路、飛行後に挿入して静止する障害物を使った統合確認である。
任意の街路での経路生成、連続飛行中の緊急回避、動く障害物、実機、荷物の配達は
検証していない。WAMの改善効果も示していない。この条件では単純な深度方式が
到達しているが、それをWAMの実用性評価の中止理由にはしない。今後は
[必要な絶対性能を満たすか](../../agents/aerial-wam-capability-evaluation.md)で評価し、
理想的な全知ルールへの勝利は求めない。今回のWAM未使用の記録から、その合否は決められない。

## 再現

固定イメージと資産は[Gateway設定](../../agents/px4-depth-gateway.md)を参照。
出力先には未作成のディレクトリを使う。ソース版は
`replay/gateway-evidence.json` の相対ファイル名とSHA-256で固定される。
元記録はローカルに別途保管しており、将来コードが変わった場合はこの版で再検証する。

```sh
export PYTHONPATH=.:packages/missionos-core/src:packages/missionos-cli/src:packages/missionos-gateway/src
export RUN_MISSION_DESIGNER_PX4_GAZEBO_SITL_EXECUTION=1
export RUN_MISSION_DESIGNER_PX4_GAZEBO_SITL_LIVE_FLIGHT=1
export RUN_PX4_URBAN_WAM_TRIAL=1
export MISSIONOS_PX4_DEPTH_ASSETS="$ASSETS"
python scripts/check_px4_depth_gateway.py --output-dir "$RUN" --scene gap --reobserve --live
python scripts/verify_urban_reobserve.py "$RUN/flights/$TASK_ID"
python scripts/export_px4_reobserve_gateway_report.py --run "$RUN" --output "$REPORT"
python scripts/check_urban_reobserve_report.py "$REPORT"
```

映像は実際のシミュレーターカメラ画像、軌跡は時刻付き観測位置。sim時刻を出発から
合わせ、表示用に約0.24秒間隔へ間引き、主要イベントと終端を残した。動画はHTTPの
byte-rangeに対応するローカルサーバーでプレビューできる。Apartment assets:
OSRF gazebo_models, CC BY 3.0, Nathan Koenig / Cole Biesemeyer。

通常タスクへの統合まで完了した。次の判断点は、この承認範囲と失敗時の記録を
PRレビューで確認し、利用可能なシミュレーター機能として扱う範囲を確定すること。
