# Starship 3D 研究モデルの実行・検証契約

この文書は `starship-v3-public-inspired-surrogate-v1` の実装契約である。
**回転する地球上の 3D 並進を積分する工学用 surrogate であり、実機を同定したモデルでも
6 自由度モデルでもない。** 公開映像との比較は診断であり、同じコードでの再計算一致を
実機に対する精度保証へ昇格させない。数値結果は実行ごとの `study.json` を参照する。

公開資料と予定・報告・映像観察の区分は [調査仕様](starship-fidelity-roadmap.md)、
[機体調査](starship-vehicle-research.md)、[映像台帳](starship-visual-research.md) にある。
旧 2D モデルと旧 bounded return の結果は書き換えない。

## 実装の分担

| ファイル | 責務 |
| --- | --- |
| `src/runtime/starship_physics.py` | WGS84 地球、J2、回転大気、層別大気、抗力・揚力、質量減少、有限推力、RK4、燃料枯渇と地表交差 |
| `src/runtime/starship_flight.py` | 決定論的誘導、2 段分離、健全性 gate、26 基の逐次放出、帰還分岐、各段・衛星の軌跡 |
| `src/runtime/starship_study.py` | 固定 request・契約・出典 hash、別プロセス実行、保存証拠の再計算、公開観測との診断比較 |
| `scripts/run_starship_3d.py` | 明示的な simulation opt-in、出力先保護、実行・再検証入口 |
| `src/runtime/starship_3d_report.py` | 保存された軌跡と判定の表示 |

LLM judges. Human approves. Rules constrain. Executor acts. Verifier checks. Repair loops.
この実装の判断器は deterministic supervisor である。LLM、Gateway、実機との接続は
行わない。CLI での simulation approval は、このローカル計算の承認だけを意味する。

## 入力の確からしさ

`reported` は運航者の公表値、`derived` はその換算、`assumption` はこの実装の設計入力を表す。
公表された**容量**を実際の搭載量と見なした部分は `assumption` のままである。

| 入力・値 | 区分と出典・限界 |
| --- | --- |
| 初期エンジン数: booster 33、Ship SL 3 + RVac 3 | `reported`。[V3 更新](https://www.spacex.com/updates#starship-v3)、[機体紹介](https://www.spacex.com/vehicles/starship)。全時刻の点火・throttle 履歴ではない |
| SL 250 tf、RVac 275 tf | `reported` の代表推力を 1 tf = 9,806.65 N で `derived`。圧力依存・実 throttle は不明。モデルでは SL 2,451,662.5 N、RVac 2,696,828.75 N の一定最大値 |
| booster 3,650,000 kg、Ship 1,600,000 kg 推進剤 | 公開紹介の容量に基づく `assumption`。対象飛行の搭載量、利用可能量、タンクごとの状態を取得したものではない |
| 直径 9 m、正面積 π × 4.5² m² | 公開寸法からの `derived`。実効抗力面積は別で、Cd は未同定 |
| 衛星 26 基 | [Flight 14 の報告](https://www.spacex.com/launches/starship-flight-14)を参照する数。モデルの ID はすべて架空 |
| 発射点 25.997° N、97.155° W、方位 90° | `assumption`。概略 Starbase 位置と東向き発射。測量済み pad datum や実際の launch azimuth ではない |
| booster/Ship dry mass 250,000/120,000 kg、衛星 1,700 kg/基 | `assumption`。対象飛行の公表質量として扱わない |
| Isp: booster 330 s、Ship 上昇 365 s、単一 SL/帰還 340 s | `assumption`。Raptor の公開 Isp map として扱わない |
| 上昇高度目標 275 km、軌道投入 perigee 250 km | 公開映像に見られた軌道高度を参考にした**設計目標**。実機の state vector、投入軌道、達成時刻の同定ではない |
| それ以外の燃料 reserve、空力、誘導、時間・故障・終端条件 | 下記の `assumption`。較正済みの SpaceX パラメータではない |

公表丸め値の SI 換算後に桁数が増えても、その分だけ入力精度が上がるわけではない。
今後の更新では世代と flight を固定し、値・分類・出典・使用した観測点を一緒に更新する。

## 固定 profile の仮定値

| field | 既定値 | 意味 |
| --- | ---: | --- |
| `booster_separation_reserve_kg` | 260,000 kg | この残量で stage を分離。公表された staging 時刻への位置合わせではない |
| `ship_ascent_reserve_kg` | 75,000 kg | 上昇燃焼を止める reserve。軌道条件が先に満たされればそこで停止 |
| `ship_return_reserve_kg` | 28,000 kg | 軌道投入・離脱で残す燃料。実機の header tank 仕様ではない |
| `ascent_cd` | 0.35 | 上昇時の固定 Cd |
| `entry_area_m2`, `entry_cd`, `entry_cl` | 400 m², 1.3, 0.22 | Ship 大気減速の固定 surrogate |
| `booster_entry_area_m2`, `booster_entry_cd` | 120 m², 0.8 | booster 空力モデル |
| `ascent_altitude_response_s` | 80 s | 高度誤差から上昇速度目標を作る controller 時定数 |
| `suborbital_perigee_target_m` | −50,000 m | SECO 判定用 osculating perigee |
| `minimum_deploy_perigee_m` | 220,000 m | 放出時に最新状態から再計算して要求する perigee |
| `deorbit_perigee_m` | 20,000 m | 離脱燃焼の cutoff 目標 |
| `orbit_gate_delay_s` | SECO 後 60 s | 健全性確認のモデル待ち時間。実際の ops timeline ではない |
| `deployment_delay_s`, `deployment_interval_s` | 45 s, 15 s | 軌道投入完了から初回、放出間のモデル待ち時間 |
| `deployment_impulse_mps` | 0.45 m/s | 軌道面法線方向の衛星 release impulse。Ship に反作用を与える |
| `early_return_orbits`, `nominal_return_orbits` | 1.65, 5.2 | 投入地点の半径で計算した円軌道周期に掛ける滞在時間係数。実周回を数えていない |
| `booster_engine_failure_time_s` | 60 s | booster 33 → 32 基の故障注入時刻。実測ではない |
| `rvac_failure_after_staging_s` | stage 後 35 s | Ship RVac 3 → 2 基の故障注入時刻。実測ではない |
| `landing_relight_altitude_m` | 1,100 m | 降下中の terminal controller 起動条件 |
| `terminal_flip_duration_s` | 3 s | 指定された姿勢・面積変化の継続時間。角運動を解いた結果ではない |
| `terminal_descent_time_constant_s` | 6 s | 高度から指令降下速度を作る係数 |
| `terminal_velocity_response_s` | 1.5 s | 鉛直速度誤差から要求加速度を作る係数 |
| `terminal_contact_target_mps` | 2 m/s | controller の降下速度目標。観測された接地速度ではない |
| `entry_bank_deg` | ±75° | 空力 lift vector の bank 指令。90 s の正弦符号で左右を変更 |
| `max_ascent_duration_s`, `max_mission_duration_s` | 750 s, 36,000 s | 上昇と Ship 計算の時間予算 |

完全な数値入力は run の `profile` に保存する。型・有限値・主要な正値・reserve の順序を検査するが、
`FlightProfile.validate()` は実機の設計実現性審査ではない。別の profile を使うときは別 run とし、
結果を見て変えた値を、事前に固定した validation 条件へ混ぜない。

## 座標系、質量、数値計算

- ECI で位置・速度を積分する。simulation t=0 に ECI/ECEF の軸を一致させ、定角速度で地球を回す。
  2026-09-28 の恒星時へ結び付けた epoch ではない。歳差・章動・極運動・地形・geoid は含めない。
- `altitude_m` は WGS84 ellipsoid に対する geodetic altitude。
  `speed_mps` / `ground_speed_mps` は `v_eci − ω × r` の **3D norm** で、水平速度だけではない。
  `radial_velocity_mps` は地心方向、`vertical_speed_mps` は楕円体法線方向を使う。
- `orbit.perigee_altitude_m` と `apogee_altitude_m` は二体 osculating element から
  WGS84 赤道半径を引いた値。geodetic altitude と定義が違う。J2/drag により時間変化する。
- 中心重力 + J2、回転大気に対する抗力と指定 bank の揚力を計算する。風はゼロ。
  86 km までは COESA 1976 の層別近似、以上は scale height 15 km の**未検証 surrogate**。
  電離層・熱圏の日変化や宇宙天気は含めない。
- state の `propellant_kg` は現在燃焼可能として表現する reservoir だけ。
  stack ascent では booster main、分離後の Ship では Ship 全推進剤である。
  `Vehicle.dry_mass_kg` はその積分区間の**推進剤以外の固定搭載質量**を意味するため、stack 時は
  booster dry + Ship 全体、Ship では dry + 未放出 payload を含む。実機の構造 dry mass と混同しない。
- thrust、推進剤消費、gravity、lift/drag から RK4 積分する。1 step 内で command は固定。
  燃料枯渇と地表接触は分割・root solve、軌道目標 crossing は有限燃焼の時間 root solve で扱う。
  到達位置や速度を目標軌道へ直接置き換えない。
- 既定 base step 1 s、coast 最大 10 s、entry 最大 0.5 s、terminal 最大 0.1 s。
  保存 trajectory の間隔は通常 5 s、衛星は最大 10 s、最終状態は必ず保存する。
  ブースターは別計算で分離から最大 1,800 s まで伝播する。

熱流束は nose radius 2 m と係数 1.7415×10⁻⁴ を仮定した Sutton–Graves proxy。
積算は heat load の診断値であり、tile 温度・厚さ・局所損傷・構造生存を判定しない。

## phase、観測、操作の意味

1. **stack ascent**: thrust direction は時間による pitch program。q > 35 kPa で throttle 0.7、
   その他 1.0。booster の推進剤消費が reserve に達したら分離する。
2. **stage separation**: 同じ位置・速度から Ship と booster の状態を分け、質量を保存する。
   イベント名 `hot_stage_separation` は工程ラベル。実際の hot staging overlap、噴流、構造負荷、
   投棄部品、分離 impulse は計算しない。
3. **Ship ascent**: 最新の高度、radial/tangential velocity、質量から有限推力の方向を指令。
   perigee 目標、reserve、時間予算のいずれかで停止。gravity を相殺する無限 thrust は使わない。
4. **suborbital coast / health gate**: SECO 後 60 s の新しい状態から高度 > 100 km、
   radial velocity > −100 m/s、燃料 > return reserve + 15,000 kg、synthetic navigation health を確認。
   `orbit_no_go` は navigation veto を注入し、SL engine が永久故障したとは扱わない。
5. **orbit insertion**: gate が許可し、radial velocity < 65 m/s で single SL engine を点火。
   perigee 250 km で停止。最新の燃料・高度が条件を外れれば中止する。
6. **deployment**: 毎回の perigee と dispenser 状態を確認して逐次放出。
   satellite は release 時に Ship と同じ位置を持つ point mass で、指定 impulse と Ship recoil を与える。
   mass と linear momentum を保存するが、実際の機体表面、PEZ 機構、衝突・再接触・姿勢は計算しない。
7. **orbital coast / return**: 計算した滞在時間後、最新の残燃料・放出数と synthetic fault history を記録して
   early/planned return を選択する。実際の着水海域への到達可能性計算や帰還 window 探索ではない。
8. **deorbit burn / coast**: single SL engine を接線逆方向に点火し、perigee または reserve 条件で停止。
   下降中に高度 < 120 km となった観測点を entry interface とする。
9. **entry / belly flop**: 固定係数の lift/drag と bank で減速。
   高度 ≤ 15 km かつ air speed < 500 m/s で belly flop phase へ移る。
10. **terminal flip / burn**: 指定高度から 3 s の指定 alpha・面積変化を与え、3 基の SL engine の
    最大推力内で垂直・水平速度の feedback を行う。ellipsoid crossing の速度を保持して contact とする。

**最小 throttle、点火遅れ、gimbal 制限、engine spool、姿勢制御能力は未モデル化**である。
terminal の 3 基には 0..1 の任意 throttle を許し、指定 flip と thrust axis を追従可能と仮定している。
この制約欠落は soft contact の実現性に直接影響する。接地速度 ≤ 5 m/s だけで実機の着水可能性を言わない。
`landing_engine_failure` でも prescribed flip/面積変化は進み、thrust だけがゼロになる。
その衝突速度はこの仮定に条件づけられ、現実の点火失敗時の姿勢・速度予測ではない。

ブースターは 4 s の指定 flip coast、13 基の boostback、固定抗力の降下、1 km 以下で 3 基の burn を
持つ独立 trajectory。実際の Flight 14 の全 engine history、帰還点、catch を再現していない。
**モデルの disposal trajectory** として示し、contact、存続、回収、再使用を別々の事実に保つ。

## 時刻一致の境界

`orbit_gate_delay_s=60`、deploy delay、滞在係数、engine-out 時刻は設計仮定である。
公式中継の T+1,551 s 付近の単一 engine burn の観察を、このモデルの投入時刻に一致した証拠として使わない。
[映像台帳](starship-visual-research.md)の時刻・高度・表示速度には読み取り・表示座標系の不確かさがある。
実測時刻、予定表、模型の phase time をそれぞれ保持する。

275 km は観測を参考にした目標として使用した。再突入・接触の映像点へ時刻や位置を直接合わせる処理はない。
一致しない区間も診断結果に残す。trajectory の見た目が似ているだけでは 3D state、空力、GNC の較正にはならない。

## 証拠と verifier の境界

`step_records.ship/booster` は各 step の before/after、要求/実 duration、Vehicle、Control、phase を保存する。
`state_transitions` は stage separation と衛星放出の明示的状態差分を結ぶ。
`release_receipts` は各衛星の ID・release state・mass・Ship recoil を保存する。
重複位置から始めた点質量が離れていくことは、実際の PEZ dispenser を検証した証拠ではない。
衛星は面積 20 m²、Cd 2.2 の仮定で passive propagation するだけで、姿勢、太陽電池、電源、RF、laser、
低推力による軌道上昇、衛星運用開始、顧客向けサービスを実装していない。

研究 verifier の固定診断条件には、衛星 26 基、perigee ≥ 180 km、apogee ≤ 450 km、追跡 ≥ 120 s、
Ship の surface tolerance 0.1 m、地表相対速度 ≤ 5 m/s を用いる。これは SpaceX の成功条件ではない。
runner の放出許可 perigee 220 km と、verifier の診断下限 180 km は異なる条件として保存する。

- `study_verified` / `evidence_verified`: 固定した計算・証拠の整合性。実機への一致ではない。
- `ship_soft_contact_candidate`: 模型の速度と prescribed alpha の条件。姿勢・生存・着水区域の検証ではない。
- `payload_orbit_verified`: 模型の保存 state に対する軌道条件。Starlink の受信・通信・運用ではない。
- `mission_completed`, `real_flight_validated`, `tps_survival_validated`, `ship_survival_verified`,
  `ship_return_zone_verified`, `satellite_service_verified`, `booster_catch_verified` は、このモデルでは真にしない。

保存 hash は変更検出と request/source binding であり、外部署名や真正な SpaceX telemetry の証明ではない。
元の失敗 run を残し、修正後は別の output directory へ実行する。

## 再現入口

新しい、未使用の出力先へ実行する例:

```sh
.venv/bin/python scripts/run_starship_3d.py --approve-simulation --scenario all --output-dir output/starship-3d-new-run
.venv/bin/python scripts/run_starship_3d.py --verify output/starship-3d-new-run/study.json
```

保存後の verification は追加の 3D 再計算を行う。外部機器や有料サービスへの接続ではない。
この文書中のコマンド例そのものは、実行済みの証拠として扱わない。
