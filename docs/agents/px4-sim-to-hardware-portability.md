# PX4/Gazebo → 実機: 持ち越しと作り直し境界

**時点:** 2026-07-24
**対象:** PX4/Gazebo SITL で実証済みの runtime を、実機（bench → HITL/cage → field）に載せるとき、何を残し何を作り直すか。

この文書は **作り直し境界の棚卸し** である。フィールド飛行の許可や安全ケースの代替ではない。
claim 語彙の正本は `claim-semantics.md`、adapter 契約は `hardware-adapter-contract.md`、
route runtime の権威境界は `px4-gazebo-route-runtime.md` を優先する。

---

## 1. 一言で言うと

```text
管制塔・claim・Recovery の判断/証拠連鎖は本線として残る。
SITL の世界・起動・sim 完了解釈は作り直し（または sim 専用に残す）。
MAVLink 実行と観測は「形は再利用・接続先と安全条件は差し替え」。
```

**「概念以外は全部捨て」ではない。**
捨てる／作り直すのは **シミュレータ実行体** と **SITL 証拠を物理成功に格上げすること** である。

現状の実機スライスは **props-removed bench の arm/disarm のみ**（`missionos_real_hardware_dispatch_runtime` + `px4_real_hardware_actuator_backend`）。
水平ルート closed-loop と Recovery の成熟は **SITL 側**にあり、そのままフィールドに載せない。

---

## 2. 段階（書き換えコストが跳ねる地点）

| 段階 | 意味 | 既存の到達 | 作り直しの主戦場 |
|------|------|------------|------------------|
| **SITL** | loopback PX4 + Gazebo | 水平ルート + Recovery まで成熟 | —（現状の本線） |
| **bench** | props removed、機体固定、物理 E-stop/電源断 | arm/disarm + adapter evidence | シリアルリンク、物理 attestation、actuator allowlist |
| **HITL / cage** | 有界アクションを 1 個ずつ、独立 safety case | 契約スキャフォールド中心 | 観測・preflight・recovery 実行バックエンド |
| **field** | 屋外・飛行・運用 volume | **未**。意図的に未主張 | ほぼ新規の安全・法規・運航パッケージ |

各段階で `execution_mode` と `completion_scope` と `physical_execution_invoked` を **別事実** として埋める。
SITL の `completed` を field の成功に読み替えない。

---

## 3. 分類ラベル

| ラベル | 意味 | 実機接続時の扱い |
|--------|------|------------------|
| **KEEP** | 権威・契約・スキーマ。差し替え不要 | そのまま本線 |
| **REWIRE** | 形・API は残す。接続先・閾値・入力源を差し替え | 薄い adapter 層で再配線 |
| **REWRITE** | sim 前提が本体。再利用は設計思想のみ | 新規実装 or sim 専用に隔離 |
| **DO-NOT-PORT-AS-PROOF** | 成果物は残してよいが証拠の強さを格上げしない | 回帰・デモ用に限定 |

---

## 4. 層ごとの判定

### 4.1 KEEP — 実機でも本線

| 領域 | 根拠・主な置き場所 |
|------|-------------------|
| Authority split | `LLM judges / Human approves / Rules constrain / Executor acts / Verifier checks / Repair loops` |
| Claim 語彙 | `claim-semantics.md`, `runtime_claim_evidence.py`, `hardware_adapter_contract.py` |
| Gateway 承認・dispatch token・task/timeline | Gateway + `TaskStore` / `artifact-taxonomy.md` |
| backend-neutral adapter フロー | prepare → approve → dispatch once → ACK/progress 分離 → scope 付き completion |
| Hardware adapter schema v1 | capabilities / preflight / candidate / approval / evidence |
| Recovery 四段パイプライン（意味） | intent → compilation → reachability → approval → dispatch-time recheck → outcome |
| Recovery intent スキーマ | `missionos_runtime_recovery_intent.v1` 等（`recovery-intent-compiler-verifier.md`） |
| 非同期 Recovery + Safety HOLD + stale 破棄 | モデル待ち中もテレメ継続；陳腐化は `superseded_retryable` |
| CLI の証拠面 | `watch` / `operate` / `map` は表示であり verifier ではない |
| 公開安全ルール | credentials / task DB / 生座標を公開物に載せない（`publication-rules.md`） |

これらを「実機だから作り直す」対象にしてはならない。壊すと MissionOS が管制塔でなくなる。

### 4.2 REWIRE — 形は残し、接続を差し替え

| 領域 | SITL での姿 | 実機で必要な差し替え |
|------|-------------|----------------------|
| MAVLink transport | container 内 loopback / embedded helper / `px4_real_mavlink_transport` | 実シリアル or 実リンク；`link_kind` で fake と区別（既に bench で一部実装） |
| 有界コマンド allowlist | SITL coupled/emergency dispatcher | 段階ごとに **許可コマンド集合を再定義**（bench は arm/disarm のみ） |
| ACK / state readback | SITL 観測ループ | 同じ分離を維持しつつ、タイムアウト・欠測・電波を field 向けに |
| Recovery **実行** | route runner / Gazebo 連携 | hardware adapter 経由の有界アクション（HOLD/RTL/LAND から） |
| Recovery **数値 envelope** | SITL 用速度・風・バッテリ・クリアランス | 機体・運用 volume・法規に合わせた再較正 |
| 障害観測入力 | Gazebo contact / collision marker / sim wind | 実センサー or partner 安全系；**トピック名ごと捨てる** |
| preflight 条件 | SITL readiness（container up 等） | 物理 E-stop、機体固定、ジオフェンス、heartbeat、attestation |
| Digital twin / route plan の「計画」側 | 地図・ルート提案 | 計画は再利用可；**dispatch 先は adapter** |
| HIL telemetry contract | `hil_telemetry_*` | 実テレメ包絡に接続（契約の骨格は KEEP） |
| Replay bundle **形式** | anonymized recovery replay | スキーマは KEEP；中身は field 証拠を新規に埋める |

### 4.3 REWRITE — 実機接続で作り直し（または sim 専用隔離）

| 領域 | 主なコード / 資産 | 理由 |
|------|-------------------|------|
| Gazebo world / SDF / payload plugin | `px4_gazebo_route/world.py`, `simulators/gazebo/`, payload detach SDF | 物理世界そのもの |
| SITL プロセス / Docker lifecycle | `runtime_lifecycle`, sitl runner, execution readiness, `digital_twin_sitl_*` | container 起動は機体電源投入ではない |
| Docker 経由の MAVLink helper 実行 | `execution.py`（`docker` 呼び出し）, `embedded_mavlink.py` の container 前提 | 実機はホスト/GCS 側 transport |
| 環境シナリオ注入 | `environment.py`, `scenario.py`, `environmental_realism.py` | wind/obstacle/visibility は **sim 注入**；field では観測に置換 |
| Contact / collision 統合 | `collision_observation.py`, `contact_integration.py`, contact smokes | Gazebo contact topic 依存 |
| SITL mission upload / live flight 巨大ランナー | `px4_gazebo_mission_designer_sitl_*.py`（live_flight ~7k 行、runner ~3.6k 行） | SITL オーケストレーション専用。field ミッション制御として転用しない |
| entrypoint の sim 結合 | `px4_gazebo_route/entrypoint.py`（~4k 行） | 正式 SITL entry；実機 entry は adapter runtime 側に新設 |
| Gazebo log / gz-sim collector | `*_log_collector*`, gazebo delivery sidecars | sim ログ経路 |
| `completion_scope=sim_action` の解釈パス | route reporting / sitl e2e smoke | field では `adapter_action` 等へ **別経路** で埋める |
| wind driver / play 系の sim 条件 | `missionos_play_wind_driver` 等 | 演出・SITL 条件；field の気象は別ソース |
| 配送完了・payload release の sim 検証 | dropoff verification, payload recovery **sim 側** | 物理配送は未主張のまま；field では独立証明が必要 |

### 4.4 DO-NOT-PORT-AS-PROOF — 持っていけるが格上げ禁止

| 成果物 | 許される用途 | 禁止 |
|--------|--------------|------|
| SITL task JSON / map HTML | 回帰、デモ、UI 確認 | フィールド成功の証明 |
| Anonymized recovery replay（SITL 由来） | 公開デモ、verifier の契約テスト | 物理実行・配送完了の主張 |
| RC notes の「SITL completed」 | sim 成熟度の説明 | outdoor flight 許可 |
| Fixture Gateway の固定タスク | 公開 clone の smoke | 実機 attestation の代替 |

---

## 5. `px4_gazebo_route/` モジュール別（おおよそ）

パッケージ合計 ~18k LOC（entrypoint 単体 ~4k）。実機では **パッケージごと移植** しない。

| モジュール | ラベル | メモ |
|------------|--------|------|
| `recovery_intent_compiler.py` | **KEEP**（閾値は REWIRE） | intent/compilation の意味保存が本線 |
| `recovery_decision_signature.py` | **KEEP** | 署名・再検証の骨格 |
| `recovery_persistence.py` / `recovery_reporting.py` / `recovery_workflow.py` | **KEEP** + 軽い REWIRE | artifact 鍵と claim を維持 |
| `recovery_outcomes.py` / `recovery_execution.py` | **REWIRE** | 観測ソースと executor を adapter に |
| `artifacts.py` / `audit.py` / `reporting.py` / `finalization.py` / `bootstrap.py` | **KEEP** 寄り | claim を壊さない限り流用 |
| `replay_bundle.py` | **KEEP** 形式 / **DO-NOT-PORT-AS-PROOF** 中身 | |
| `configuration.py` | **REWIRE** | env ゲート名と既定値 |
| `route_decision.py` / `operational_verification.py` / `operational_outcomes.py` | **REWIRE** | 判定ロジックは有用；入力が sim 前提 |
| `observation.py` / `verification.py` / `supervision.py` | **REWIRE** | 監視の型は残す；リンク損失は docker 経由から実リンクへ |
| `terminal_action.py` / `normal_route_flow.py` / `alternate_route.py` | **REWIRE**〜**REWRITE** | フロー骨格は参考、dispatch は SITL runner 結合 |
| `execution.py` / `embedded_mavlink.py` | **REWRITE**（transport 知識は抽出可） | docker + container helper が本体 |
| `world.py` / `scenario.py` / `environment.py` / `environmental_realism.py` | **REWRITE**（sim 専用） | |
| `collision_observation.py` / `contact_integration.py` / `dynamic_observation.py` | **REWRITE** | Gazebo 固有 |
| `compound_hazard_transition.py` | **REWIRE**（概念） / **REWRITE**（sim 遷移） | multi-hazard の状態機械は Action Feasibility 側へ移植候補 |
| `payload_recovery_flow.py` / `route_deviation_flow.py` | **REWIRE**〜**REWRITE** | 意味は残るが sim 観測に密結合 |
| `runtime_lifecycle.py` | **REWRITE** | SITL 起動/停止 |
| `entrypoint.py` | **REWRITE**（実機用 entry を別置） | SITL 正式境界として維持 |

隣接の巨大 SITL 専用（route パッケージ外）:

| ファイル | おおよそ LOC | ラベル |
|----------|-------------:|--------|
| `px4_gazebo_mission_designer_sitl_live_flight_run.py` | ~7,200 | **REWRITE** / sim 専用維持 |
| `px4_gazebo_mission_designer_sitl_runner.py` | ~3,600 | 同上 |
| `px4_gazebo_mission_designer_sitl_delivery_epic_exit.py` | ~700 | 同上 |

---

## 6. 既にある実機スライス（作り直しの「種」）

これらは **field ではない** が、REWIRE の正解方向を示している。

| 部品 | 役割 | 限界（意図的） |
|------|------|----------------|
| `px4_real_hardware_actuator_backend.py` | 承認後 arm/disarm、ACK + state readback | takeoff / mission start を承認に含めない（`takeoff_allowed=False`） |
| `missionos_real_hardware_dispatch_runtime.py` | Gateway token → backend、hardware adapter evidence 投影 | opt-in env なしでは inert |
| `px4_real_hardware_mavlink_reader.py` / `readonly_target` | 読み取り専用観測 | コマンド面を持たない |
| `px4_real_mavlink_transport.py` | フレーム encode 等 | リンク開閉・安全ケースは呼び出し側 |
| `hardware_adapter_runtime.py` + registrations | 中立 registry / runner / verifier | PX4 飛行アクション一式は未登録 |
| Hardware adapter contract の PX4 bench 投影 | capabilities〜evidence | `completion_scope=adapter_action` は **実シリアル bench のみ** |

**実機に繋いだ瞬間にゼロからではない。**
ゼロからになるのは **「SITL ルートランナーを飛行に流用する」発想** のときである。

---

## 7. Recovery Agent の分解

| 層 | ラベル | 実機メモ |
|----|--------|----------|
| 戦略語彙（monitor / local_avoidance / hold / rtl_or_land 等） | **KEEP** | 機体非依存の判断語彙 |
| 提案のみ（承認・dispatch にならない） | **KEEP** | 絶対に崩さない |
| intent → compiler → reachability → outcome の artifact 連鎖 | **KEEP** | schema と hash 連鎖 |
| 非同期推論 + HOLD + stale discard | **KEEP** | field でより重要 |
| ホスト LLM 呼び出し配線 | **REWIRE** | レイテンシ・通信断を field 向けに |
| コンパイル後の幾何・速度・クリアランス | **REWIRE** | 機体・センサー依存で再較正 |
| Gazebo contact 起因の avoid トリガ | **REWRITE** | 入力源を置換 |
| SITL runner への recovery dispatch | **REWRITE** | adapter 有界アクションへ |
| SITL で `avoid_obstacle` が通った事実 | **DO-NOT-PORT-AS-PROOF** | field で再証明 |

TurtleBot3 側は **別 intent スキーマ**（`turtlebot3-recovery-contracts.md`）。
「Recovery を一本化して全ロボット共通実装」は、**語彙と claim は共有・実行は adapter 分割** が正しい。

---

## 8. アーティファクトの分解

| カテゴリ | ラベル | 実機メモ |
|----------|--------|----------|
| Claim 付き schema（hardware evidence, recovery intent/compilation/reachability/outcome） | **KEEP** | 同じ語彙で field を書く |
| Task / timeline | **KEEP** | append-only 監査 |
| Map / watch HTML | **KEEP**（表示） | verifier にしない |
| `missionos_runtime_recovery_*`（SITL ラン中身） | **DO-NOT-PORT-AS-PROOF** | 形式は再生産、中身は新規 |
| Anonymized replay bundle スキーマ | **KEEP** | 公開境界（座標除去等）は維持 |
| SITL e2e smoke result（`physical_execution_invoked=False` 固定） | sim 専用 | field 用 schema を別途または mode で分離 |
| Fixture task | 公開 smoke 用 | 実機 attestation に使わない |

---

## 9. 「実機に繋いだら作り直し」チェックリスト

実機 PR / スライスを切るとき、次を明示する。

### 作り直す（または新規）

- [ ] 実行 entrypoint（SITL `px4_gazebo_route.entrypoint` を呼ばない）
- [ ] リンク開閉と `link_kind` 証明（real serial vs fake）
- [ ] 物理 attestation（props removed / 固定 / E-stop / 電源断 の段階定義）
- [ ] 許可アクション allowlist（段階 0: arm/disarm のみ、など）
- [ ] preflight 理由集合（telemetry_stale, heartbeat_lost, geofence, … + 物理条件）
- [ ] 観測パイプライン（Gazebo contact を使わない）
- [ ] Recovery **dispatch バックエンド**（route runner 外）
- [ ] 閾値・envelope の機体別設定
- [ ] `execution_mode` ∈ {bench, hitl, cage, field} と `completion_scope` の埋め方
- [ ] 独立 safety case と runtime smoke（契約テストだけでは不足）

### 作り直さない（壊したら欠陥）

- [ ] proposal ≠ approval ≠ dispatch ≠ ACK ≠ progress ≠ delivery ≠ physical
- [ ] Recovery が自己承認・自己 dispatch しないこと
- [ ] ACK だけで completion にしないこと
- [ ] sim / loopback 証拠を `physical_execution_invoked=true` にしないこと
- [ ] 公開物に task DB・生 WGS84・秘密を載せないこと

### やってはいけないショートカット

- [ ] `px4_gazebo_mission_designer_sitl_live_flight_run` を実機ミッション制御に転用
- [ ] SITL `completed` を field リリースゲートにする
- [ ] LLM に raw MAVLink / 無制限 setpoint を渡す
- [ ] takeoff / mission start / payload release を bench スライスに混ぜる
- [ ] Recovery 提案を operator 承認なしで executor に直結

---

## 10. 推奨する実機ロードマップ（実装順）

作り直しコストを抑え、KEEP を活かす順序。

```text
1. bench arm/disarm（既存）を維持・強化
     物理 attestation・fail-closed・adapter evidence を崩さない
2. 読み取り専用テレメ（既存 readonly）を field センサー品質へ
3. 有界 safe アクション 1 個（HOLD または RTL 相当）のみ adapter 登録
     intent/compiler/outcome 連鎖をその 1 アクションで通す
4. LAND 等を同様に 1 個ずつ
5. avoid / reroute はセンサーと幾何が揃ってから
6. mission upload / AUTO.MISSION / 配送は最後
     ここはほぼ新規の運航・安全パッケージ（SITL ランナーの移植ではない）
```

SITL の水平ルート + Recovery は、この階段の **上の方の振る舞いをシミュレータで先に壊さないための実験場** であり、
**階段そのものの実装資産ではない。**

---

## 11. 規模感（価値と LOC の違い）

| 区分 | おおよその製品価値 | LOC の目安（参考） | 実機時 |
|------|-------------------:|-------------------:|--------|
| KEEP（管制塔・claim・Gateway・Recovery 骨格・adapter 契約） | 高 | 契約・gateway・compiler 中心 | そのまま |
| REWIRE（transport 形、観測型、数値 envelope） | 中 | mavlink transport / observation / bench 数百〜千行級 | 差し替え実装 |
| REWRITE（SITL オーケストレーション） | sim 成熟には高・field には低 | route ~18k + live_flight/runner ~11k | **移植しない** |

「SITL の何割の行がコピペで動くか」ではなく、**どの事実を実機で再証明するか** で切る。

---

## 12. 関連ドキュメント

| 文書 | 使うとき |
|------|----------|
| `px4-gazebo-route-runtime.md` | SITL route の権威境界・モジュール所有 |
| `hardware-adapter-contract.md` | bench/adapter evidence・禁止アクション |
| `hardware-partner-integration-guide.md` | partner adapter PR の最小セット |
| `backend-neutral-adapter-runtime.md` | 中立 registry / conformance |
| `recovery-intent-compiler-verifier.md` | Recovery 四段の schema |
| `claim-semantics.md` | フィールド辞書 |
| `replay-bundle-contract.md` | 匿名 replay の公開境界 |
| `real-hardware-bridge.md`（concepts） | 人間向けの狭い bridge 説明 |
| `repository-status.md` | リポジトリ全体の成熟度スナップショット |

---

## 13. 変更時ルール

- この文書の KEEP を弱める変更は、claim / adapter / recovery 契約の更新とセットにする。
- SITL 専用モジュールに field 用分岐を増やし続けるより、**adapter 境界の外に実機 entry を置く**。
- 新しい実機アクションは「allowlist 1 個 + preflight + evidence + opt-in smoke」を最小単位とする。
- PR の `E2E / Runtime Verification` には、**どの段階（bench/hitl/field）か** と **physical claim の有無** を必ず書く。
