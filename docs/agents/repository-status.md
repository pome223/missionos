# MissionOS リポジトリ現状レビュー

**時点:** 2026-07-25
**公開タグ:** `v0.1.1` (`public/main` @ `e255f5e`)
**ローカル注意:** この文書の作成元 `main` @ `a454e65` は public/main の祖先であり、
公開履歴より 14 commit 遅れている。ブランチ位置は更新前に再確認すること。

この文書はリポジトリ全体の現状スナップショットである。機能契約や claim 境界の正本は、
各トピック専用の `docs/agents/` / `docs/concepts/` 文書を優先する。

---

## 一言で言うと

**「LLM にジョイスティックではなく管制塔を渡す」制御プレーン**として、authority
分離（提案 / 承認 / 制約 / 実行 / 検証）が設計の中心に据えられた早期公開
スナップショットである。シミュレータ上の closed-loop（特に PX4/Gazebo と
TurtleBot3）は実証が進み、**物理実行・配送完了は意図的に未主張**のままである。

---

## 1. 位置づけと成熟度

| 項目 | 状態 |
|------|------|
| バージョン | public は **v0.1.0 stable** と **v0.1.1** を公開済み |
| ライセンス | Apache-2.0 |
| 公開方針 | 公開準備中の private 扱い（`AGENTS.md` / publication rules） |
| 主経路 | `MISSIONOS_GATEWAY_BACKEND=production missionos chat --autostart` |
| 主 LLM | DeepSeek V4（`deepseek-v4-flash`）、Gemini / Ollama もサポート |
| 検証 | v0.1.0 stable gate が PX4/Gazebo と TurtleBot3/Nav2 の Core conformance と live simulator evidence を機械可読に固定 |

### 実行トラック（証拠境界付き）

v0.1.0 stable gate は PX4/Gazebo と TurtleBot3/Nav2 を同一 Core 契約で
`verified_feasible` と記録する。ただし両 backend は simulator evidence であり、
`physical_execution_invoked=false` を維持する。

| トラック | 進捗 | ブロック要因 |
|----------|------|--------------|
| **PX4 / Gazebo SITL** | Core の `verified_feasible`、人間承認、dispatch-time revalidation、operator surface parity を stable gate で固定 | 実機飛行・配送完了・physical execution は未 |
| **TurtleBot3 / Nav2** | 同一 Core の `verified_feasible` と屋内 sim baseline を stable gate で固定 | 実機 TB3 / E-stop 未 |
| **TurtleBot4** | プロファイル・契約はある | Create3/Gazebo で意味のある `/odom` が出ない |
| **Nova Carter / Isaac** | opt-in スキャフォールド | ライブ Isaac 証拠なし |
| **Nvblox** | 証拠契約・ゲートあり | ライブデータなし |

参照:

- `README.md` — Current Status / Runtime Progress
- `docs/releases/v0.1.0.md` — stable gate の証拠と限界
- `docs/agents/evidence/20260724-v0.1.0-stable-readiness.json` — 2-backend stable gate の機械可読 summary
- `docs/concepts/simulator-baseline.md` — シミュレータ baseline の境界
- `docs/agents/px4-sim-to-hardware-portability.md` — PX4 SITL→実機の持ち越し/作り直し境界

---

## 2. リポジトリ構造

```text
packages/          # 公開パッケージ境界（cli / gateway が実体、core・sitl は薄い）
src/               # 本体（runtime が圧倒的に大きい）
scripts/           # smoke(77) / audit / bridge / replay など py ~135 本
tests/contract/    # 契約テスト中心（~132 files）
docs/concepts/     # 人間向け抽象説明
docs/agents/       # エージェント・メンテナ向け契約
docs/examples/     # 公開デモ説明
docs/releases/     # RC notes
data/, output/     # ローカル実行物（gitignore、未追跡）
```

### コード規模（おおよそ、2026-07-24 再計測）

| 領域 | 規模感 | 読み方 |
|------|--------|--------|
| `src/runtime` | 205 py / ~138k LOC | **モジュール群の合計**。単一モノリスではない |
| `src/gateway` | ~34 py / ~30k LOC（`server.py` が 12,386 行） | 残る大型モジュールのひとつ |
| `packages/*` | CLI 中心（`cli.py` 4,258 行 + 分割モジュール） | CLI 整理は public 反映済み |
| `scripts` | 135 py / ~54k | **全体**。うち `smoke_*.py` は 77 本 / 29,495 行 |
| `tests` | ~47k LOC | 契約テスト中心 |
| 合計 `.py` | ~670 files | — |

#### モノリス整理の現状（訂正）

初版レビューは「整理は internal のみで public 未反映」と誤認していた。
2026-07-24 の再確認では、整理済み対象は public `main` と internal `develop` で
主要 blob が同一である。

| 対象 | 整理前（inventory 記録） | 現在の public / internal |
|------|--------------------------:|--------------------------:|
| CLI 本体 `cli.py` | 12,866 行 | **4,258 行** |
| CLI モジュール | モノリス中心 | **25 モジュール** |
| PX4 旧 Smoke 兼本体 | 12,634 行級 | **20 行の互換 wrapper** |
| PX4 正式 entrypoint | 未分離 | **4,005 行 + 38 モジュール**（`px4_gazebo_route/`） |
| `smoke_*.py` | 135 本・52,033 行 | **77 本・29,495 行** |

主要ファイルの blob は public/main と origin/develop で同一（2026-07-24 確認）:

- `packages/missionos-cli/src/missionos_cli/cli.py`
- `src/gateway/server.py`
- `src/runtime/px4_gazebo_route/entrypoint.py`
- `scripts/smoke_px4_gazebo_horizontal_route_delivery.py`（互換 wrapper）
- `src/runtime/digital_twin_mission_environment.py`

公開への反映経路（例）:

- public PR #17: PX4 runtime 分割 + Smoke 統合
- public PR #18: 正式 runtime entrypoint
- public PR #29: CLI 分割 + Smoke 整理

**正確な評価:**

> PX4・CLI・Smoke のモノリス整理は **public へ反映済み**。
> ただし次の巨大モジュールは残っている。

| 残る大型モジュール | おおよそ行数 |
|--------------------|-------------:|
| `src/gateway/server.py` | 12,386 |
| TurtleBot3 runtime（`turtlebot3_home_mission.py`） | 10,241 |
| Digital Twin environment | 9,961 |
| PX4 live-flight runtime | 7,226 |

唯一、internal の詳細棚卸し `docs/agents/codebase-inventory.md` は public に無い。
コード整理は済んでいるが、経緯と削減値をレビュー担当が把握しにくい。
public 向けの短い構造説明を足す価値がある。

| パッケージ | 成熟度 |
|------------|--------|
| `missionos-cli` | 実体あり。分割済み（chat / operate / map / companions など 25 モジュール） |
| `missionos-gateway` | 薄いサーバラッパ（本実装は `src/gateway` 側。`server.py` は依然大きい） |
| `missionos-core` | claim_semantics 程度の薄い共有層 |
| `missionos-sitl` | ほぼスタブ |

---

## 3. 設計上の強み

1. **Claim discipline がプロダクトそのもの**
   proposal ≠ approval ≠ dispatch ≠ ACK ≠ progress ≠ landing ≠ delivery ≠
   physical。境界が README / concepts / agents / tests / release notes まで一貫。

2. **ドキュメントが二層**
   - 人間: `docs/concepts/`, `docs/examples/`
   - エージェント: `docs/agents/`（変更対象ごとの読了マップあり）
   AI コーディング向けに運用が意識されている。

3. **公開安全ルールが明文化**
   credentials / task DB / 生成物 / ローカル絶対パスを持ち込まない。
   ハードウェア・SITL は opt-in、fixture 優先。

4. **検証文化**
   - CI: Python 3.11 / 3.13、contract suite、限定 ruff、smoke inventory
   - PR には runtime smoke と `E2E / Runtime Verification` 必須
   - v0.1.0 stable は fresh public clone の acceptance と 2-backend Core
     conformance を release evidence に固定している

5. **最近の機能的到達点（rc.1 → v0.1.1）**
   - 複合 hazard（wind + obstacle）recovery
   - 非同期 recovery 推論（推論中も telemetry / HOLD 継続）
   - 陳腐化結果の `superseded_retryable` 破棄
   - dispatch 時 telemetry 仲裁の fail-closed
   - DeepSeek を primary / 標準 install に
   - CLI boundary smoke、匿名 recovery replay 公開

Authority split の正本:

```text
LLM judges.
Human approves.
Rules constrain.
Executor acts.
Verifier checks.
Repair loops.
```

---

## 4. リモート・ブランチ・作業場の状態

| 項目 | 状態 |
|------|------|
| `public/main` | `e255f5e` (`v0.1.1`) |
| ローカル `main` | `a454e65`。`public/main` の祖先であり、14 commit 遅れ |
| `origin`（internal） | public とは別履歴として扱い、同期前に祖先関係を確認する |
| ローカルブランチ | **~103** |
| worktree | 多数（public/internal 実験用が並存） |
| 次の設計対象 | simulator evidence を物理実行に読み替えない bench conformance boundary |

ローカルには `data/` 約 2.8GB、`output/` 約 1.4GB の実行成果物がある
（gitignore 済みで公開リポには入らない想定）。開発マシン上のノイズとしては大きい。

Remote:

- `origin` → `pome223/missionos-internal`
- `public` → `pome223/missionos`

---

## 5. 次の本線

PR #96 の Action Feasibility は完了し、v0.1.0 stable gate は PX4 と Nav2 の
両方で Core 契約を通ることを固定した。したがって次の信用ギャップは robot 種別では
なく、**simulator から物理 bench への境界**である。

最小の候補は、プロペラを外して固定した PX4 bench の arm/disarm のみを、第三の
conformance backend として通すこと。既存の real-hardware runtime を再利用しつつ、
real serial link、物理 E-stop、固定 attestation、action allowlist、dispatch-time
revalidation、ACK と state readback を fail-closed で結ぶ。成功しても
`completion_scope=adapter_action` に限定し、flight、delivery、mission completion を
主張しない。

SITL world、lifecycle、Gazebo 観測、Docker MAVLink helper はこの slice に流用しない。
KEEP / REWIRE / REWRITE の境界は `px4-sim-to-hardware-portability.md` を正本とする。

---

## 6. リスクと技術的負債

### 高

| リスク | 内容 |
|--------|------|
| **残る大型モジュール** | Gateway `server.py`、TB3 home mission、Digital Twin、PX4 live-flight はなお 7k–12k 行級。次の分解対象 |
| **構造整理の文書ギャップ** | 整理コードは public にあるが `codebase-inventory.md` が public に無く、レビューが「未整理」と誤読しやすい |
| **パッケージ境界が未完** | インストールは packages 経由でも、実体の多くは `src/`。`missionos-core` / `sitl` は薄い |
| **二重リモートの履歴分岐** | public と origin は単純な fore/behind ではなく分岐しうる。同期手順を誤ると混線リスク |
| **ブランチ / worktree 過多** | 実験線が多数残存。正本の認知負荷が高い |

### 中

| リスク | 内容 |
|--------|------|
| **README 進捗表の陳腐化** | stable gate を README に反映する変更が必要（この snapshot と同時に更新する） |
| **CI の ruff 範囲が部分的** | 「maintained boundaries」のみ。runtime 全体は lint 網羅ではない |
| **契約テスト偏重** | `tests/contract` 中心で e2e は docs/opt-in smoke。フル SITL はローカル/人手依存 |
| **legacy / multi-domain コード** | desktop/browser/computer_use/memory など、管制塔コアと別系統が同居 |
| **ローカル成果物肥大** | 4GB 超の data/output。公開汚染は gitignore で防いでいるが、ディスクと誤コミットリスクは残る |
| **scripts 総量と smoke の混同** | scripts 全体 ~135 本は audit/bridge 等を含む。`smoke_*.py` は 77 本に削減済み |

### 低〜製品上の既知ギャップ

- 物理実行・HITL フィールド未
- TB4 / Nova Carter / ライブ Nvblox 未
- Recovery delegation は experimental
- delivery completion は常に未主張（意図的）

---

## 7. 品質シグナル（良い点）

- **fail-closed が一貫したデフォルト**（telemetry 不一致、stale inference、
  policy drift 等）
- **証拠言語がプロダクト UI・ログ・ドキュメントで揃っている**
- **公開 clone からの再現を RC で毎回書いている**（install 品質の意識）
- **fixtures / 匿名 replay** で「見せられる証拠」と「生の task DB」を分離
- disclaimer が具体的で、過大宣伝を構造的に避けている

---

## 8. 総評

| 観点 | 評価 |
|------|------|
| ビジョンと一貫性 | **非常に強い**。authority split が設計・実装・文書・検証まで貫通 |
| シミュレータ実証 | **PX4 / Nav2 の 2-backend Core conformance は stable gate 済み** |
| 公開準備 | **v0.1.0 stable と v0.1.1 を公開済み**。秘匿物の分離ルールは明確 |
| コードベース衛生 | **中〜良**。PX4/CLI/Smoke 整理は public 反映済み。Gateway/TB3/Digital Twin 等が次 |
| 本番/実機 | **意図的に未**。研究・参照・sim 制御プレーンとして正しい自己位置 |
| 次の価値 | safe bench adapter を第三の conformance backend として通し、物理実行の限定的な証拠を作る |

**総合:**
v0.1.0 stable は、同一 Core 契約が PX4 と Nav2 の実行前 feasibility を fail-closed
に扱えることを示した。一方で stable gate 自身は authority を作らず、両 backend の
`physical_execution_invoked=false` を明示する。次に必要なのは機能数や robot 種別の
拡張ではなく、プロペラを外した固定 bench で限定 action を実行し、その physical
boundary を既存の authority / evidence 契約に正しく接続することである。

---

## 9. 推奨アクション（優先度順）

1. **公開 README を v0.1.0 stable の 2-backend Core conformance に更新**する。ただし
   simulator evidence を physical execution に読み替えない
2. **bench conformance corpus を凍結**する。allowlist、fake link 拒否、attestation、
   preflight fail-closed、claim scope を先に決める
3. **existing PX4 hardware adapter を Core 契約へ REWIRE**し、SITL runner は REWRITE
   対象のまま残す
4. **opt-in live bench E2E を 1 往復だけ実行**し、ACK と state readback を独立に記録する
5. 上記が再現可能な evidence として揃って初めて **3-backend gate** を判断する

---

## 10. 関連ドキュメント

| 読みたい内容 | 文書 |
|--------------|------|
| 人間向け境界 | `docs/concepts/boundaries.md` |
| claim 意味論 | `docs/agents/claim-semantics.md` |
| 公開ルール | `docs/agents/publication-rules.md` |
| E2E 検証要件 | `docs/agents/e2e-verification.md` |
| stable release | `docs/releases/v0.1.0.md` |
| 2-backend stable evidence | `docs/agents/evidence/20260724-v0.1.0-stable-readiness.json` |
| エージェント読了マップ | `docs/agents/README.md` |
| エージェント作業規則 | `AGENTS.md` |

---

## 更新履歴

| 日付 | 内容 |
|------|------|
| 2026-07-23 | 初版。`main` @ `b6e0c41` / `v0.1.0-rc.3` 時点の現状レビューを記録 |
| 2026-07-24 | **訂正:** モノリス整理は public 反映済み。`src/runtime` 合計行と scripts 全体を単一モノリス/Smoke と誤読していた点を修正。残る大型モジュールと inventory 文書ギャップを明確化 |
| 2026-07-25 | **更新:** public `v0.1.0` stable / `v0.1.1`、2-backend Core gate 完了を反映。次の本線を sim→physical bench に更新 |
