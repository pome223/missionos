# Starship 6自由度モデルの公開根拠と再現範囲

調査日: 2026-10-04。対象は Flight 14 を参照する V3 Ship / V3 Super Heavy / Raptor 3。これは実装・検証の要求仕様であり、実行済み証拠ではない。一次資料索引は [starship-sixdof-sources.json](../../examples/spaceflight/starship-sixdof-sources.json)、飛行資料の詳細は [機体調査](starship-vehicle-research.md) と [ミッション調査](starship-mission-research.md) にある。

## 到達点と区別

対象は、発射台から上昇、hot staging、booster帰還、Ship軌道投入、衛星放出、離脱、空力降下、反転、接触までを、同じ物理状態から連続的に進める**3次元・可変質量の6自由度シミュレーション**である。姿勢を描画用に補間するだけ、所定高度で速度を置換するだけ、着水成功をイベント時刻から決めるだけでは要件を満たさない。

「公開されている物理を実装できる」と「実際のStarshipを精度保証して再現できる」は別の主張。次の軸を混ぜない。

| 軸 | 値と意味 |
| --- | --- |
| `model_origin` | `public_physics`: 公開方程式、`generic_approximation`: 条件付き工学近似、`operator_reported`: 事業者公表、`spacex_unknown`: 今回の資料で確定できないモデル・値 |
| `implementation_status` | `not_implemented` / `implemented_unverified` / `verified_generic`。コードの存在だけで最後へ進めない |
| `vehicle_validation_status` | `not_validated` / `compared_to_public_observations`。汎用方程式の検証から実機検証へ昇格しない |
| `parameter_origin` | `public_constant` / `reported_nominal` / `declared_assumption` / `unknown`。代表仕様は飛行時実測値ではない |
| `observation_role` | `input` / `calibration` / `held_out_evaluation` / `display_only`。同じ観測を調整と独立評価に兼用しない |

本調査時に読んだ従来の `src/runtime/starship_physics.py` は3D point massであり、`State3D`に姿勢・角速度・慣性がない。大気は86 km超を明示した指数近似である。新しい実装のカバレッジは、別の実行証拠とコード版に結び付けて更新する。この研究文書は並行実装を完了扱いしない。

## 世代を固定する公表事実

[SpaceX現行紹介ページ](https://www.spacex.com/vehicles/starship)をブラウザーで再確認した。全高124 m、直径9 m、Ship 52 m / 推進剤容量1,600 t、booster 72 m / 3,650 t、Ship 3 SL + 3 RVac、booster 33基（内側13基可動）が代表仕様。容量から実際の初期搭載量・reserve・混合比を推定して確定値にしない。

[2026-05-12 V3更新](https://www.spacex.com/updates#starship-v3)は、boosterのgrid finが3枚、統合hot stage、Shipの後方flap actuatorが各1台・3 motors、RCS・長時間coast用推進剤管理の更新、Raptorの公表推力SL 250 tf / RVac 275 tfを示す。旧世代4枚grid finやFAAの評価最大構成をこの機体へ混ぜない。tfからNへの換算で桁を増やしても、元の公表値の精度は増えない。

[Flight 14飛行後記録](https://www.spacex.com/launches/starship-flight-14)は、上昇中のbooster 1基停止、Ship RVac 1基停止と残り5基による燃焼延長、booster再点火本数、Ship単発SLによる軌道投入・離脱、26機放出、短縮運用と北太平洋着水を公表している。横に掲載された約9時間50分のtimelineは `All Times Approximate` の予定系列であり、早期帰還の実時刻には使わない。エンジン故障を「全燃焼で永久故障」の1ビットに圧縮せず、燃焼別の開始・停止・再点火を持つ。

## 優先する物理モデルと検証

以下は同じミッションへ接続する具体的な要求。優先度Aは6DOFとして欠かせない結合、Bは公開式を追加できる精密化、Cはデータ入手を必要とする拡張である。優先度は「実装済み」を意味しない。

| ID / 優先 | モデルと最低限の状態・出力 | 根拠と独立検証 | 固有値・残る限界 |
| --- | --- | --- | --- |
| M01 / A | 慣性系位置・速度、body-to-inertial quaternion、body角速度。力とトルクを同じ積分段階で評価 | NASA NESC、NASA RP-1262。無トルク自由回転・定トルク・力とトルク同時印加 | SpaceXの慣性・内部質量分布は未知。quaternionを目標姿勢へ直接代入しない |
| M02 / A | WGS84 geodetic座標、地球自転、central gravity + J2。慣性速度・地上相対速度・対気速度を別保存 | NESC atmosphere 05–10、orbital 02。SPICEのframe/time定義 | 一様自転を実装してもITRF93/J2000精密変換とは呼ばない。地球姿勢データなしのepoch整合限界を表示 |
| M03 / A | 各タンクの残量・位置、機体CG、正定値3×3慣性。燃料使用と質量・慣性・モーメント腕を結合 | NASA RP-1262 §3、Basilisk fuelTank。空タンク・非対称排出・角運動量収支 | 主タンク/header形状、LOX/CH4配分、内部流路、sloshは未知。単なるmass比例慣性は近似 |
| M04 / A | エンジン別燃焼状態、有限推力・燃料流量、SL/RVac別圧力依存、throttle/ramp/restart、位置・向き | NASA推力式と理想rocket equation、SX-V3。真空有限燃焼の解析解・燃料枯渇境界 | Raptor Isp/throttle/点火過渡/再点火包絡は未公開。公表推力のみでマップ全体を作ったとしない |
| M05 / A | gimbal、flap、grid fin、RCSのcommanded/achieved状態、角度・速度・遅れ・飽和。各点の力からCGまわりトルクを合算 | NASA TVC研究は機械・構造結合の必要性を示す。step/rate-limit/engine-out・左右差試験 | NASA SLS actuatorの数値はRaptorへ流用しない。無制限の「要求トルク」を直接実現する制御は除外 |
| M06 / A | Mach/迎角/横滑り/角速度/制御面依存の3力・3モーメント。力の作用点をCGから区別 | NASA APAS II, pp.70–72・Appendix C。対称性・panel mesh収束・sphere/plate解析解 | hypersonic modified-Newtonianは形状依存の近似。亜音速・遷音速・剥離流・grid fin干渉へ無条件外挿しない |
| M07 / A | 下層US76の温度・圧力・密度・音速、geopotential/geometric高度区別、風を対気速度へ反映 | US76の表、NESC風ケース | 86 km以上の固定scale-heightはUS76全高度実装ではない。上層モデル・適用領域・切替連続性を記録 |
| M08 / A | 分離前stackと分離後2機のmass/CG/姿勢/角速度/相対位置、有限hot-staging overlap、分離impulse | NESC atmosphere17、線形・角運動量保存 | 段間噴流荷重は別問題。NASA-HSのCFD相当とは主張しない。接触解除時の速度を任意にリセットしない |
| M09 / A | attitude feedback、制御配分、飽和下の追従誤差、ascent/insertion/deorbit/entry/flipの閉ループ制御 | 汎用制御として試験。初期姿勢偏差・engine-out・windから目標到達／失敗を観測 | SpaceX GNC・FDIR・gain schedulingは未知。参照軌跡、誘導目標、物理状態は別変数 |
| M10 / B | 停滞点対流熱流束と積分熱量、公開物性を使う場合の熱伝導・放射・境界温度 | Sutton–Graves適用条件、NASA TPS。熱収支・解析熱伝導問題 | qdotだけでtile/hinge温度や生存を判定しない。物性・厚さ・接触抵抗・局所乱流遷移は別の未知量 |
| M11 / A | 放出の実行、Ship質量・反力、各satelliteの独立3D軌道と識別子。相対速度・接触なし・軌道条件を別検証 | 運動量保存、軌道伝播比較、SX-F14の放出実績 | PEZ内部機構・押出し速度・V3質量/慣性は未知。放出のみでcheckout・通信・orbit raise・serviceを完了にしない |
| M12 / A | 地表/海面との最初の接触をroot solveし、直前r/v/q/ω/m・接触点・時刻を保存 | dt収束・傾いた機体先端の接触・地表貫通反例 | 設定した着水包絡は実機強度限界ではない。浮力・水撃・倒壊・chopsticks構造を未実装ならcatch/reuseは未検証 |
| M13 / B | sensor観測とtruthを分離、遅延・欠測・量子化・bias設定、navigation estimate、通信可用性の記録 | 既知入力と観測時刻・age検査、no-future-information試験 | 公開HUDから実機IMU/GNSS/noise/FDIR閾値を同定しない。誤差0のtruth navigationも明示した近似 |
| M14 / C | 地域・季節・高度の大気変動、熱圏・太陽活動依存、風の空間/時間変動 | NASA Earth-GRAM2026の資料・配布条件・実出力を取得後に照合 | 文書を読んだだけでGRAM実装済みにしない。乱数の一様ばらつきを気象観測として扱わない |
| M15 / C | tank settling/slosh、構造弾性・TVC連成、局所熱・損傷、plume interference | 公開汎用multibody/CFD/FEMで個別checkcaseを作る | Starship固有geometry/material/testデータなしで予測を保証しない。高詳細meshだけでは不足 |

M01–M12の「A」を落としたまま全行を「6DOF完成」と表示しない。一方、Cが未実装でもAの数値検証に価値はある。カバレッジは完成/未完成の一つの色にせず、物理モデル・パラメーター・数値検証・飛行比較の列を保つ。

## 実装で間違えやすい契約

### 座標・時間・角運動量

内部単位はSI、角度はrad。quaternionの成分順・回転方向・積の順序、body軸、風の座標系、CGと幾何原点をschemaで固定する。Euler角は表示用派生量とする。対気速度は自転大気と風を差し引いて計算する。ECEF状態をECIと呼ぶ、位置だけを回転して速度変換項を落とす、geocentric高度をgeodetic高度に見せることを反例にする。[NAIF frames](https://naif.jpl.nasa.gov/pub/naif/toolkit_docs/C/req/frames.html)

可変質量の回転を、閉じた剛体のEuler方程式に `I(t)` を代入しただけで一般式としない。`I_dot`、移動CG、内部流れ・流出角運動量を、採用したcontrol volumeと整合させる。どれを省くかを公開する。[NASA RP-1262](https://ntrs.nasa.gov/citations/19910015989) pp.53–55はjet dampingの流路依存性を扱う。[Basilisk fuelTank](https://avslab.github.io/basilisk/Documentation/simulation/dynamics/FuelTank/fuelTank.html)もmass-property更新だけと追加depletion結合を区別する。異なる近似同士の差を積分器の誤差として評価しない。

### 推進と操舵

推力と流量のモデルを一貫させる。`F = mdot*Ve + Ae*(pe-pa)`を使うか、同じ圧力条件のeffective `Isp`から`mdot = F/(g0*Isp)`を使う。圧力補正を二重計上しない。燃料枯渇・点火・消炎・分離は積分区間内のeventとして処理し、負残量を最後に0へ丸めて余分なimpulseを与えない。[NASA推力式](https://www1.grc.nasa.gov/beginners-guide-to-aeronautics/rocket-thrust-equation/)

姿勢controllerの要求は実現されたトルクではない。actuator出力から実現値を計算し、飽和・追従誤差を残す。姿勢誤差があると、同じthrottleでも推進方向と軌道が変わらなければならない。flap無効化・片側固着で姿勢が必ず元へ戻るなら、隠れた姿勢強制を疑う。NASA SLSの[実測に基づくTVC研究](https://ntrs.nasa.gov/citations/20230000427)はモデル構造の参考であり、SpaceX actuatorのパラメーター源ではない。

### 空力・熱の適用範囲

[APAS II Appendix C](https://ntrs.nasa.gov/citations/19910013767)のmodified-Newtonian panel近似は、法線・面積・流れとの角度から圧力を積分できる公開手法である。係数表はMach/alpha/beta/control stateと有効域を持ち、外挿を黙って継続しない。機体法線力を出しても、leeward pressure、粘性、遷音速、flap shock interaction、希薄気体は別のモデルが必要。`Cd=constant`を外見のmeshだけで精密化したことにしない。

[Sutton–Graves](https://ntrs.nasa.gov/citations/19720003329)は軸対称鈍頭の停滞点対流加熱が対象。`k*sqrt(rho/Rn)*V^3`を用いる場合、`k`の単位・大気組成・壁温の仮定、局所曲率、連続流の適用条件を明記する。これを全タイルの温度・損傷確率へ直結させない。熱伝導を加える場合もSpaceXの非公開物性をNASA一般材料の値で事実化しない。

## 未知量を設定可能にする形式

各設定項目は少なくとも次を持つ。数値が必要な実行は、利用者が選んだ仮定セットを凍結し、そのhashを結果へ保存する。`unknown`の値は`null`であり、0ではない。

```json
{
  "parameter_id": "ship.dry_mass_kg",
  "value": null,
  "units": "kg",
  "parameter_origin": "unknown",
  "vehicle_scope": "flight14_ship_v3",
  "source_ids": [],
  "uncertainty": {"kind": "not_quantified", "range": null},
  "assumption_reason": null,
  "sensitivity_outputs": ["insertion_propellant", "entry_attitude_error", "contact_state"]
}
```

優先して露出する未知量は、(1) dry mass/CG/inertia、tank形状とLOX/CH4/header/reserve、(2) engine位置・Isp・throttle/ignition/gimbal制限、(3) aero coefficient/CP/flap/grid-fin効力、(4) TPS物性・厚さ・境界、(5) navigation/FDIR/guidance、(6) separation/PEZ impulse・satellite特性。geometryが公式写真からの推定なら、その推定手順・写真時点・誤差を別記する。

範囲を知らない量へ「95%信頼区間」や確率分布を発明しない。仮定した上下値での決定的な感度比較は可能だが、それは実機の発生確率ではない。観測へ合わせたパラメーターは`calibration`として保存し、複数の未知量を同じ少数HUD点へ合わせて実機同定と呼ばない。

## 検証の段階と完成条件

1. **方程式と数値**: 解析解・保存則・dt収束。quaternion norm、エネルギー、運動量、正定値慣性、燃料会計を監査する。姿勢は`q`と`-q`が同じ回転であることを考慮して比較する。
2. **外部の再現可能な比較**: [NASA NESC 2015](https://nescacademy.nasa.gov/flightsim/2015)の訂正済みcheckcaseと初期条件・結果を使用する。atmospheric 02/03で回転と減衰、05/07/08で自転と風、17で二段、orbital 08/09で自由回転と力・トルクを優先する。2013版から条件が変わり、2015版にもerrataがある。採用データをchecksum固定する。
3. **別実装**: native Basiliskと同じ初期条件・同じ力/質量近似を連続積分して比較する。後続サンプルを再初期化に使わない。比較範囲を姿勢・角速度にも広げる。共通のaero係数を与えた一致は、その係数自体を検証しない。
4. **全ミッション結合**: launch→separation→両機→deployment→deorbit→contactを一つのrunで進める。姿勢誤差、制御面固着、engine-out等の事前設定でterminal outcomeが変わることを確かめる。成功・失敗の両方を保存する。
5. **公開飛行との比較**: 公表event、映像読み取りの高度・速度・姿勢・時刻の精度を区別し、調整に使わなかった点で差を報告する。同高度の通過時刻、区間所要時間、軌道要素、姿勢変化を別に評価する。時刻差補正はモデルの物理精度向上と呼ばない。

実行結果には `sixdof_integrated`、`external_dynamics_comparison_passed`、`full_mission_executed`、`public_observation_comparison_available`、`starship_vehicle_validated=false`、`physical_execution_invoked=false`を別々に記録する。`6DOF`は自由度の説明であり、CFD/FEM、実機flight software、実運用の認証を意味しない。結果は実際に通過した段階まで主張する。

MissionOSはこの状態に対し、軌道投入許可、放出継続/保留、帰還機会の選択等を扱う。[SpaceXの公開運用設計](https://www.spacex.com/updates#orbital-starship)にある健全性確認を参照できるが、その実機thresholdは未公開である。LLM/Jevを姿勢の内側loopへ入れず、提案・承認・Rules・executor・独立verifierを分ける。この役割分離と、モデルが単純ルールを上回ったかの比較は別の評価である。

## 文書と証拠の扱い

この変更で保存するのは索引・短い要約・独自の設計契約のみ。SpaceXページの全文、他機関の大規模モデルデータ、非公開生成結果は取り込まない。NASA/AVSの文書が読めても、そのsoftware/dataの再配布条件を一括して推定しない。ソース索引の`license_or_rights`は読んだ資料の表示と未確認を区別する。

研究文書自体の検証はJSON parse、source IDの参照整合、ローカルリンク、`git diff --check`。数値・native実行・全ミッションの合否は実装側のrunで記録する。本調査はそれらを実行したと主張しない。
