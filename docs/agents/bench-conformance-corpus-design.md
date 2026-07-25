# PX4 bench conformance corpus 設計

**時点:** 2026-07-25
**位置づけ:** 設計案。実装前。実機 E2E は未実行。
**前提:** v0.1.0 stable gate（PX4/Gazebo + TurtleBot3/Nav2 の 2-backend Core conformance）が既に成立している。

この文書は、**プロペラを外して固定した PX4 bench** を第三の conformance backend として
Core 契約に載せるための corpus 設計である。フィールド飛行の許可、安全ケース、
運航パッケージの代替ではない。

正本の優先順位は次のとおり:

- claim 語彙 → `claim-semantics.md`
- adapter 契約 → `hardware-adapter-contract.md`
- 既存 corpus の様式 → `action-feasibility-conformance-corpus.md`
- SITL→実機の持ち越し境界 → `px4-sim-to-hardware-portability.md`

---

## 1. なぜ corpus から始めるか

v0.1.0 で閉じたのは **ロボット種別の軸**（空 PX4 × 地上 Nav2 が単一 Core 契約を通る）である。
開いたままなのは **sim→物理の軸**で、stable gate の全 backend が
`physical_execution_invoked: false` を報告している。

bench slice はこの軸を、**最小の物理面積**で開ける。既に存在するもの:

| 資産 | 場所 |
|------|------|
| bench 用 capabilities / preflight / candidate / approval / evidence builder | `src/runtime/hardware_adapter_contract.py`（`build_px4_bench_hardware_adapter_*`） |
| 実機 dispatch runtime | `src/runtime/missionos_real_hardware_dispatch_runtime.py`（498 行） |
| actuator backend | `src/runtime/px4_real_hardware_actuator_backend.py`（814 行） |
| 実シリアル MAVLink reader | `src/runtime/px4_real_hardware_mavlink_reader.py` |
| adapter conformance contract | `tests/contract/test_hardware_adapter_conformance.py` |
| 実機 smoke | `scripts/smoke_missionos_real_hardware_arm_disarm_route.py` ほか |

**足りないのは機能ではなく接続である。** この slice は Core の tri-state、
dispatch-time revalidation、conformance corpus を通っていない。したがって
新規実装ではなく **#103（Nav2）と同型の adapter 接続作業**として扱う。

---

## 2. 決定的な設計上の注意（先に読むこと）

`packages/missionos-core/src/missionos_core/conformance.py` の `_AUTHORITY_OUTPUTS` は
`physical_execution_invoked` を含み、全ケースで `False` を要求する
（`run_conformance_corpus` L90、違反時 `adapter_created_authority`）。

**この不変条件を bench のために緩めてはならない。**

理由: このフラグは *replay* が何を起こしたかを述べている。corpus の replay は
シリアルポートを開かず、機体に触れず、何も arm しない。したがって bench corpus でも
`physical_execution_invoked=false` が**正しい**。

物理の事実は corpus ではなく、次の 2 か所に入る:

| 層 | 物理の扱い |
|----|-----------|
| conformance corpus | `physical_execution_invoked=false` を維持。物理の来歴は `truth_boundary.runtime_truth.source_runtime_evidence_available=true` と `source_execution_mode="bench"` で表現 |
| live bench E2E + stable-readiness JSON | ここで初めて `physical_execution_invoked: true` / `completion_scope: "adapter_action"` を記録 |

Core に `if bench:` の分岐が入ったら中立性の後退である。設計レビューで弾くこと。

---

## 3. Corpus の所在と様式

既存 2 backend と同じ配置にする。

```text
tests/golden/action_feasibility/px4_bench_v1/manifest.json
tests/golden/action_feasibility/px4_bench_v1/cases/*.json
```

生成・検証コマンドも同型:

```bash
PYTHONPATH=. .venv/bin/python scripts/generate_px4_bench_action_feasibility_corpus.py
PYTHONPATH=. .venv/bin/python scripts/smoke_action_feasibility_conformance_corpus.py
PYTHONPATH=. .venv/bin/python -m pytest -q tests/contract/test_px4_bench_action_feasibility_conformance_corpus.py
```

adapter id: `missionos.px4_bench.action_feasibility.v1`
adapter module（新規）: `src/runtime/px4_bench_core_action_feasibility_adapter.py`

manifest / case は既存と同じ二重ハッシュ（`case_sha256` + manifest 側 `sha256`）と
publication sanitation（`task_*`、TaskStore フィールド、絶対パス、秘匿値、
prompt/model 応答、owner/session 識別子の拒否）を継承する。

---

## 4. ケース設計

bench の hazard は「風・障害物」ではなく **物理安全条件**である。
したがって refusal 群は PX4 SITL の写しではなく、bench 固有に再定義する。

action は `HardwareActionKind.PX4_ARM_DISARM_BENCH` の 1 種のみ。
`allowed_actions` はこれと `SAFE_STOP` に限定し、`RAW_MOTOR` / `RAW_MAVLINK` /
`PX4_OFFBOARD_SETPOINT` / `BOUNDED_LOCAL_MOVE` は `blocked_actions` に明示列挙する。

### 4.1 正方向（1 件）

| case_id | 内容 |
|---|---|
| `px4-bench-positive-verified-arm-disarm` | preflight `passed`、Core `verified_feasible`、人間承認 → dispatch-time revalidation `valid` → ACK `accepted` → state readback で armed 観測 → disarm → `completion_scope=adapter_action` |

正方向ケースが凍結すべき観測:

- 実シリアルリンク（`link_kind` が fake/loopback ではない）
- 物理 E-stop の可用性と機体固定 attestation
- プロペラ非装着の確認
- ACK と state readback を**別事実**として保持（`ack_is_execution_effect=false`）
- `mission_completion_claimed=false`、`delivery_completion_claimed=false`
- flight / altitude / progress を一切主張しない

### 4.2 拒否方向（7 件）

すべて **fail-closed** であること。「観測できないなら拒否」であり、
「観測できないので楽観」は許さない。

| case_id | 凍結する結果 | 何を守るか |
|---|---|---|
| `px4-bench-refusal-estop-unavailable` | `blocked` | 物理停止手段のない dispatch を作らない |
| `px4-bench-refusal-vehicle-not-secured` | `blocked` | 固定 attestation なしに arm しない |
| `px4-bench-refusal-props-attached` | `blocked` | プロペラ装着の申告があれば bench slice を拒否 |
| `px4-bench-refusal-loopback-link-kind` | `unverified` | SITL/loopback リンクを実機証拠に格上げさせない（最重要） |
| `px4-bench-refusal-stale-telemetry` | `unverified` | 陳腐化テレメトリでの dispatch を拒否 |
| `px4-bench-refusal-action-not-in-allowlist` | `blocked` | `bounded_local_move` 等が allowlist 外として拒否される |
| `px4-bench-refusal-heartbeat-loss` | `unverified` | heartbeat 欠落中の承認・dispatch を拒否 |

`refusal-loopback-link-kind` は他の 6 件と性格が違う。これは機体を守るのではなく
**証拠の格を守る**。`px4-sim-to-hardware-portability.md` の
`DO-NOT-PORT-AS-PROOF` を機械可読にしたものであり、bench corpus の核心である。

### 4.3 truth boundary の埋め方

| フィールド | 正方向 | 契約由来の拒否ケース |
|---|---|---|
| `source_runtime_evidence_available` | `true` | `false`（`source_contract_evidence_refs` を代わりに置く） |
| `source_execution_mode` | `"bench"` | ケースに応じて |
| `runtime_invoked_by_this_replay` | `false` | `false` |
| `source_runtime_reexecuted` | `false` | `false` |

live bench 実行前は正方向ケースも `source_runtime_evidence_available=false` で
着地させ、**回帰ベースラインとして先にマージしてよい**。実機 E2E の成立後に
再シールして `true` へ引き上げる。これは #100 が PX4 で採った順序と同じである。

---

## 5. 実施順（issue 分割案）

```text
#105 bench conformance corpus 凍結（契約由来。実機不要）
  -> #106 hardware adapter を Core 契約へ接続（parity 証明）
  -> #107 bench live E2E（arm/disarm 1 往復、実シリアル）
  -> #108 3-backend stable gate = v0.2.0
```

| issue | 完了条件 |
|---|---|
| #105 | **完了（2026-07-25）**。§7 のサニタイザ 3 規則を先に追加し、8 ケースが `run_conformance_corpus` で `verified`。実機・ネットワーク・LLM 不要 |
| #106 | **完了（2026-07-25）**。`missionos.px4_bench.action_feasibility.v1` が Core 経由で 8 ケース通過。既存実機ランタイム由来の判定が corpus と一致（parity）。`packages/missionos-core/` への変更 **0 行** |
| #107 | 実シリアル 1 往復。`physical_execution_invoked=true`、`completion_scope=adapter_action`。flight/delivery/mission completion は false のまま |
| #108 | stable-readiness JSON に 3 つ目の backend。CI が 3 corpus 全部を回し、1 つでも落ちれば stable を塞ぐ |

#105 と #106 は**実機なしで完了できる**。ここまでを先に出すのが安全かつ速い。

### #106 の実装メモ（2026-07-25）

橋渡しは `src/runtime/px4_bench_core_feasibility_bridge.py`。
`missionos_real_hardware_dispatch_runtime` が**すでに出している** preflight と
physical attestation を Core の `HazardState` / `ActionCandidate` に翻訳する。
新しい preflight は実装していない。

実装中に「未確立を観測済みと誤読する」罠が**一段下でも再発**した。

| 層 | 罠 | 対応 |
|----|----|------|
| attestation | `Literal[True]` のため危険が「事実の欠落」として現れる | 3 ケースを `unverified` に retarget（§4.2 参照） |
| preflight builder | 安全 3 フィールドの**デフォルトが `False`**。意味は「未確立」であって「観測された危険」ではない | `False` を転送せず落とし、`unverified` に着地させる |

どちらも「沈黙を観測として報告しない」という同一原則である。新しい層を足すたびに
同じ確認をすること。

橋渡しで守った境界:

- **loopback 昇格の遮断** — 情報源は**接続自身のラベル** `link_kind`。
  `mark_connection_real_serial()` は実シリアル opener からのみ呼ばれ、意図的に
  非エクスポートなので、呼び出し側が fake を real と偽れない。actuator backend は
  `physical_execution_invoked == (link_kind == real_serial_pymavlink)` を
  モデル不変条件として強制しており、bridge はその権威を継承する。
  ラベル無し（unlabeled）はどのクラスにも解決せず `unverified`
- **公開境界** — serial device、`attesting_operator_id`、`bench_photo_evidence_ref` は
  hazard state に入れない。`adapter_parameters` も転送しない（不透明な mapping は
  デバイスパスが将来紛れ込む経路）
- **ドリフト防止** — ランタイム側と corpus 側の観測事実名の集合一致を契約テストで要求

---

## 6. この slice で触らないもの

`px4-sim-to-hardware-portability.md` の REWRITE 群には一切手を出さない。

- Gazebo world / SDF / payload plugin
- SITL プロセス・Docker lifecycle、`digital_twin_sitl_*`
- Docker 経由の MAVLink helper 実行
- 環境シナリオ注入（wind / obstacle / visibility）
- contact / collision 統合
- SITL live-flight ランナー（~7k 行）と route entrypoint（~4k 行）

bench entry は SITL entrypoint の分岐ではなく、**adapter runtime 側の別経路**である。

---

## 7. 公開判断：public に出す（条件付き）

**決定（2026-07-25）: `px4_bench_v1` は public に出す。**

根拠。bench の契約アーティファクトは `hardware_adapter_contract.py` の
`build_px4_bench_hardware_adapter_*` を見るかぎり **enum と boolean のみ**で構成される。

- `adapter_parameters={}`（arm/disarm はパラメータを持たない）
- `safety_constraints_applied` は制約**名**の列挙であって値ではない
- `max_speed_mps=0.0` / `max_altitude_m=0.0` / `max_distance_m=0.0`

むしろ公開したほうが「飛ばさない slice である」ことの証明になる。
`publication-rules.md` の Never Import 6 項目に該当するものはない。

### ただし bench が新しく持ち込む漏洩経路が 3 つある

既存サニタイザ（`src/runtime/px4_gazebo_route/action_feasibility_corpus.py`）は
credential / `task_*` / 絶対パス / owner 識別子 / prompt を弾くが、以下は**素通りする**。
**#105 の一部として先に塞ぐこと。** corpus 生成より前に実施する。

| 経路 | 現状 | 必要な対応 |
|------|------|-----------|
| シリアルデバイスパス `/dev/tty.usbmodem14201`、`COM3` | `_ABSOLUTE_PATH_PATTERN` は `/Users`, `/private`, `/tmp`, `/home`, `/var/folders` のみ。`/dev/` も `COM<n>` もマッチしない | パターンに `/dev/` と `^COM\d+$` を追加 |
| autopilot ハードウェア UID（PX4 `AUTOPILOT_VERSION.uid`）、ボードシリアル | 該当キーが `_FORBIDDEN_KEYS` に無い。個体識別子そのもの | `autopilot_uid` / `board_serial` / `hardware_uid` を `_FORBIDDEN_KEYS` へ追加。どうしても必要なら不可逆ハッシュのみ |
| `approval_actor` | 自由文の識別フィールド。`_FORBIDDEN_KEYS` に無い | **キーごと禁止**。「実名かどうか」を汎用判定する手段がないため、固定トークン強制ではなくキー自体を弾く。承認者の存在を記録したい場合は `approval_actor_class: "maintainer_fixture"` のように身元ではなく区分を持たせる |

1 つ目が最も効く。`px4-bench-refusal-loopback-link-kind` ケースは `link_kind` を扱うため、
**デバイスパスが自然に紛れ込む位置**である。`link_kind` は `"serial"` / `"loopback"` という
**クラス**のみを保持し、パス・USB シリアル番号・ポート番号を保持しないことを契約で縛る。

既存 corpus と同様、**サニタイズ規則ごとに禁止物を再シール後に注入する契約テスト**を書く。
古い整合ハッシュが先に落ちることで検査が素通りする事故を防ぐため。

### 実装結果（2026-07-25）

規則は `src/runtime/corpus_publication_sanitation.py` に**共有モジュール**として実装した。
実装時に、Nav2 が独自の弱いコピー（正規表現のみ、禁止キー 9 個のインラインセット）を
持っていることが判明したため、bench が 3 つ目のコピーを作らないよう PX4 / Nav2 の
両方をこの共有モジュールへ接続した。bench corpus は `publication_findings()` を
import するだけでよい。

副次的に Nav2 は `artifact_path` / `db_path` / `owner_session_id` / `file://` /
Windows 絶対パスも弾くようになった（既存 5 ケースに衝突なし）。

`COM\d+` は値全体または `\\.\COMn` デバイス形式にアンカーしてある。素の `\bCOM\d+\b`
では `COMPLETED` のような散文に誤爆するため。**誤爆しないことの契約テスト**
（`link_kind: "serial"` / `"loopback"`、`COMPLETED` を含む散文、相対 `raw_logs_ref`）も
併せて置いた。bench corpus は link の**クラス**を記録するので、ここが通らないと
設計そのものが成立しない。

## 8. 操作者申告への依存（解決済み・2026-07-25）

各ケースは `verifier_assumptions` ブロックを持ち、観測事実を 2 つに分類する。

| 分類 | 事実 |
|------|------|
| `machine_observed_facts` | `link_kind`, `link_declaration_consistent`, `heartbeat_alive` |
| `operator_declared_facts` | `physical_estop_available`, `vehicle_physically_secured`, `power_disconnect_available`, `operator_physically_present`, `props_removed_attested` |

**下段の 5 つは、コードが一切検証しない。** 物理 E-stop が存在するか、それが試験
済みか、機体が本当に固定されているか、プロペラが本当に外れているかを確認する機械的
手段は無い。これらは名前付きの操作者がそう言ったから成立している。

この分類は散文ではなく**検査対象**である。`verify_px4_bench_corpus_case` は
分類漏れの事実を `bench_corpus_fact_unclassified` で fail-closed に落とす。
新しい attestation 由来の事実が、黙って測定値の重みを獲得することを防ぐため。
`operator_declared_facts_are_machine_verified` は常に `false` で、`true` を
主張すると `bench_corpus_operator_declaration_overclaimed` で落ちる。

`notes` に記録した運用上の落とし穴:

- **PX4 の Safety Switch は物理 E-stop ではない。** 前者は arming 許可の操作、
  後者はソフトウェアが失敗しても止められる独立手段。混同した操作者は、bench slice が
  前提するより弱いものに対して「正直に」申告してしまう
- **USB 給電中はバッテリ回路を開いても機体は de-energize されない。**
  `power_disconnect_available` はオートパイロットに実際に給電しているレールを指す

### 残る未解決

1. **live E2E の物理準備の正本をどこに置くか。** プロペラ非装着・固定・E-stop 配線の
   チェックリストはコード契約ではない。`hardware-partner-integration-guide.md` の
   拡張か、独立の bench 運用手順書か。

---

## 更新履歴

| 日付 | 内容 |
|------|------|
| 2026-07-25 | 初版。#105 相当の corpus 設計案。実装・実機 E2E とも未着手 |
| 2026-07-25 | 公開判断を追記。`px4_bench_v1` は public 前提。ただしシリアルデバイスパス・autopilot UID・`approval_actor` の 3 経路を #105 の先頭で塞ぐことを条件とする |
| 2026-07-25 | #105 完了。サニタイザ共有モジュール化 + `px4_bench_v1` 8 ケース凍結。`packages/missionos-core/` への変更 0 行。全スイート 1397 passed。実機 E2E は依然未実行 |
| 2026-07-25 | attestation 拒否 3 件を `blocked` → `unverified` に retarget。`Literal[True]` により「観測された危険」が表現不可能なため |
| 2026-07-25 | #106 完了。実機ランタイム → Core の橋渡しと parity 証明。preflight の `False` = 未確立問題を同時に修正。全スイート 1414 passed。実機 E2E は依然未実行 |
| 2026-07-25 | link 判定の権威を `execution_mode`（呼び出し側申告）から `link_kind`（接続自身のラベル）へ移管。「BENCH と申告しつつ fake connection」を `blocked` として検出する 9 番目のケースを追加。実機 E2E 前に必要な修正 |
