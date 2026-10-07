# Starship 公開資料に基づく再現仕様

調査基準日: 2026-10-03。これは3Dモデル実装前にまとめた**設計仕様**であり、記載した
項目すべての実装や検証完了を示すものではない。現在の実装範囲と実行結果は
[3Dモデルの契約](starship-3d-contract.md)を参照する。
基準ミッションは **Flight 14 の V3 機体**とする。
旧型の成功、将来構成、飛行前の予定を、この飛行の実績に合成しない。

2026-10-04: 姿勢を直接指定する段階とは別に、[6自由度の開発実装](starship-sixdof-contract.md)
を追加した。quaternion・角速度・有限作動・剛体分離を実行し、NASAの回転部分とBasiliskで
数値照合する。これは以下の全仕様の完成や、旧Flight 14残差の解消を意味しない。

人向けの入口は [調査結果](../examples/starship-research.md)。個別根拠は
[機体](starship-vehicle-research.md)、[飛行履歴](starship-mission-research.md)、
[映像観察](starship-visual-research.md) を参照する。

## 調査から決めた再現の範囲

最初に作るべきものは、公開情報で照合できるイベント・判断分岐と、取得できた観測点の
範囲で軌道を照合する Flight 14 参照モデルである。SpaceX の内部 GNC、空力データベース、エンジンモデルを
持つ digital twin と呼ばない。以下の三つを別の成果物にする。

1. **観測再生**: 公式映像で読んだ点、SpaceX が報告したイベント、未観測の区間を表示する。
2. **力学シミュレーション**: 固定した仮定から積分した状態を表示し、観測点との残差を出す。
3. **MissionOS 監督**: 新しい観測から続行・放出保留・早期帰還などを判断し、承認範囲と
   Rules を満たした操作だけを executor へ渡し、結果を別途検証する。

観測値の補間を simulator output と呼ばない。飛行結果を知っている脚本の分岐を
LLM の能力向上と呼ばない。現在の deterministic supervisor は LLM を使っていない。

現段階の[再突入診断と監督の境界](starship-entry-diagnostics.md)では、物理パラメータの
追加調整を止め、次段を提案・承認範囲・Rules・実行receiptのfixture検証として整理した。
現在の5条件は故障処理の回帰条件であり、LLMの改善余地を測った比較ではない。
候補の違いを調べる入口は[放出詰まりの有限モデル比較](starship-dispenser-experiment.md)。
これは実機再現のパラメータを変更しない別の合成実験である。

[チャット実行の契約](starship-mission-chat-contract.md)では、計画提案と個別承認を分け、
固定catalogのローカル実行・保存記録の検証へつなぐ入口を追加した。
Jevの故障時分類は[観測だけを受け取るシャドー](starship-jev-shadow-contract.md)として接続した。
新しい飛行観測に対するLLMの反復判断やJevによる実行変更は未接続であり、
この入口の実装を上記3の継続的監督の完了とは扱わない。

公開する運用の範囲は[放出監督](starship-flight-supervision.md)と
[チャット実行契約](starship-mission-chat-contract.md)に限定する。
別の宇宙運用テレメトリ調査を、Starshipの監督改善や実機復旧の実績へ加算しない。

## 一次資料と読み取りの扱い

| 資料 | 確認した箇所 | 採用する範囲 |
| --- | --- | --- |
| [SpaceX Flight 14](https://www.spacex.com/launches/starship-flight-14) | 2026-09-28 の飛行後本文、Countdown、Flight Timeline。JavaScript 描画後の本文を確認 | 本文は運航者報告、表は approximate な予定。actual time series ではない |
| [SpaceX Starship to Orbit](https://www.spacex.com/updates#orbital-starship) | 2026-09-15、Initial orbit design | 初期 suborbital、health gate、軌道投入、継続監視、帰還候補の設計 |
| [SpaceX Introducing Starship V3](https://www.spacex.com/updates#starship-v3) | 2026-05-12、各 Change Highlights | 世代固有の機体・エンジン・発射台変更。性能目標と実測値を区別 |
| [Starlink Version 3 Satellites](https://starlink.com/ls/updates/starlink-version-3-satellites) | 本文をブラウザーで確認。記事日付表示なし | V3 用通信・太陽電池仕様。個々の飛行衛星のサービス実績ではない |
| [NASA 6 DOF verification check cases](https://ntrs.nasa.gov/citations/20150001263) | NASA/TM-2015-218675 Vol I、abstract | 数式・座標系・環境モデルを独立に検証する方法。Starship 校正資料ではない |
| [NASA Earth GRAM 2026](https://ntrs.nasa.gov/citations/20260004497) | NASA/TM-20260004497、abstract、2026-05-01 | 地域・季節・高度・風・変動を扱う大気モデル候補。今回は取得・実行していない |
| [CelesTrak GP data formats](https://celestrak.org/NORAD/documentation/gp-data-formats.php) | Background、Implementation | OMM/JSON、SGP4 mean elements、epoch/frame の扱い。今回 26 基の軌道を取得・照合したわけではない |
| [Starlink trajectory API](https://space-safety.starlink.com/docs/api/get-all-trajectories/) | Request、Responses | operator ephemeris / optical、hypothetical / definitive / candidate の区別。API の実データ取得は未実行 |

FAA の環境評価・図面のページ番号、SpaceX の過去飛行資料は個別の調査ノートに記載する。
検索結果の抜粋だけでは数値を採用しない。取得に失敗した映像区間は未観測のまま残す。

## 現行モデルから変更する境界

| 境界 | 現行コードの状態 | 次の実装と検証対象 |
| --- | --- | --- |
| 上昇 | `spaceflight_dynamics.py` の quintic tracking。180 s に分離、600 s に 300 km 円軌道へ誘導 | 質量が変わる 2 段上昇、姿勢、有限推力、engine out、初期 suborbital。高度と速度だけでなくイベント順序を照合 |
| 分離と booster | 2D 状態を複製し、独立の理想化帰還 | separation 時の質量・位置・速度・姿勢を保存。hot staging、boostback、空力降下、landing burn を別 phase にする |
| 軌道投入 | 上昇中に目標円軌道へ直接誘導 | 船体の健全性評価後の有限時間の single sea-level engine burn。orbit go がない場合は suborbital に留まる |
| 放出 | 900 s に架空の 4 基へ位置差・速度差を与える | 26 基を個体管理する sequential dispenser。姿勢、開扉、放出 impulse、再接触、機体質量変化、個別 receipt |
| 軌道滞在 | 短い中心重力 coast | 回転地球上の ground track、姿勢・電力・通信・推進系の健全性、帰還 window。J2/drag の影響を段階的に比較 |
| 離脱 | 1500 s に瞬間的な 180 m/s impulse | 残燃料・姿勢・再点火条件を伴う有限 burn。承認済み海域の到達可能性を再計算 |
| 帰還 | 約 120 km から空力抵抗と推力上限を加えた同じ tracking。lift/attitude なし | entry、bank、flap 制御の aerodynamic descent、flip、landing burn、surface contact を分離 |
| 終端 | 位置・速度の接地条件 | 到達地点・地表相対速度・姿勢・角速度・機体状態。splashdown/catch/recovery/reflight を別に記録 |

現在の bounded return の 3 ケース失敗は、旧誘導と仮定に対する負の結果として保存する。
これは Starship の帰還不能を意味しない。乾燥質量や推力だけを調整して同じ軌跡を通すことを
「実機に近づいた」と評価しない。[数値モデルの契約と限界](starship-sixdof-contract.md) はそのまま保持する。

## 力学と機体の実装仕様

以下は**こちらの実装提案**であり、SpaceX の内部実装を述べたものではない。

### 座標系と環境

- ECI で状態を積分し、epoch と地球回転を用いて ECEF・緯度経度・地表相対速度を出す。
  geocentric altitude と geodetic altitude を明示する。水面到達を単なる原点付近の高度ゼロにしない。
- 空力には `v_air = v_eci - omega_earth × r - wind_eci` を使う。画面に表示された speed の
  座標系が未確認なら、ECI の速度に直接 fitting しない。
- 最初は追跡可能な標準大気＋明示した風の仮定。次に Earth GRAM 等による分散を評価する。
  NASA guide が公開されていることは、モデル配布条件の確認や runtime 導入の代わりにならない。
- 空気密度を地表から宇宙まで単一の scale height で推定する現行近似は較正対象から外す。
  希薄流、連続流、遷音速、亜音速で近似の適用範囲を記録する。

### 推進と質量

- Ship/booster それぞれの dry mass、payload、LOX/CH4、header reserve、重心・慣性を分離する。
  capacity と実際の搭載量、engine rated thrust と実際の commanded thrust を別 field にする。
- engine ごとに SL/RVac、健全性、点火/停止/再点火、throttle、gimbal、有限応答、消費流量を持つ。
  不明な throttle floor、Isp curve、再点火遅れ等は仮定の範囲として残す。
- 分離、放出、燃料枯渇、接地を event として時間刻みの途中で処理する。質量・運動量・単位を検査する。
- vacuum engine failure を SL landing engine failure と同一に扱わない。残ったエンジンで
  延長燃焼できても、共通原因故障の懸念や reserve は独立に判断する。

### 空力と姿勢

- まず 3D translation + prescribed attitude の段階で lift/drag と bank を導入し、
  `attitude_model=prescribed` を明示する。これを 6 DOF と呼ばない。
- 次に quaternion、角速度、慣性、空力 moment、flap actuator、TVC/RCS の 6 DOF を実装する。
  `CL/CD/Cm = f(Mach, alpha, beta, flap)` を入れ、公開資料のない係数表は surrogate として扱う。
- GNC は決定論的 controller と executor に置く。LLM が毎ステップの gimbal や flap を
  指示する構成にしない。機体世代・質量・入口状態の不確かさを振った envelope で評価する。
- V3 hot staging の近傍流れを単純な separation impulse で代表させる場合、plume impingement
  や構造熱負荷は未モデル化と記録する。詳細 CFD の代用にしない。

### 熱と接地

- Sutton Graves の stagnation heat flux はスクリーニング指標として残すが、タイル温度、
  接着部、flap hinge の局所加熱、破損を直接判定しない。
- TPS を追加する場合は層ごとの材料特性・厚さ・境界条件・放射・損傷モデルの出典を用意し、
  少なくとも別の熱計算と照合する。公開値が不足する限り `tps_survival_validated=false`。
- 水面付近では ground-relative vertical/horizontal velocity、attitude、angular rate を検証する。
  splashdown を tower catch に置き換えない。catch は塔との相対位置、捕捉幾何、接触荷重を
  持つ別モデルとする。水面着地後の残存と回収も別の観測が必要。

## 衛星は放出後も別ミッションとして扱う

V3 の公式仕様は six 400 Gbps lasers、1 Tbps downlink、160 Gbps uplink などの設計能力を
記している。一般 technology ページの別世代の数値を V3 にそのまま入れない。
能力の数字から、各衛星のリンク確立や顧客向け通信の成功を推定しない。

MissionOS の提案状態機械は `stowed → released → separated → acquired → checked_out →
orbit_raising → on_station → service_verified`。これは研究用設計であり、Starlink 内部の
運用状態名ではない。各衛星について state vector、epoch、ID、電力/姿勢、通信相手、
receipt、期限を保存する。デモの ACK を実際の ground station 接続と表示しない。

近傍分離は Ship と衛星群の相対運動で検査し、軌道上昇には低推力・姿勢・電力を加える。
Flight 14 の正確な deployment orbit、衛星 mass、低推力 profile、運用開始時刻は、今回
確認した資料だけでは入力値として確定できない。以前の 300 km 円軌道を実績として流用しない。

将来 GP/OMM を照合する際は、26 基と catalog ID の対応、データ取得時刻、要素 epoch、
TEME/ECI 変換、SGP4 の意味を検証する。mean elements を osculating elements として
二体積分器に投入しない。追跡軌道だけで通信サービスの可用性を証明しない。

## ミッション判断と完了契約

SpaceX の公表内容では、軌道投入前の健全性確認と、軌道上での継続/早期帰還の判断がある。
MissionOS ではこの構造を参照しつつ、次の**研究用契約**へ明示的に落とす。

| 判断点 | 入力となる新しい観測 | 許可する分岐 | 必要な終端証拠 |
| --- | --- | --- | --- |
| 軌道投入前 | SL engines、姿勢、航法、タンク圧、通信、帰還 reserve | orbit go / passively safe suborbital continuation | burn receipt と更新された軌道 |
| 放出前・放出間 | orbit、dispenser、衛星数、相対運動、姿勢 | deploy next / inhibit remaining | 個体別 release と separation |
| coast 中 | 残推進剤、圧力、電力、熱、通信、故障履歴 | continue / approved early return | 選択した window・海域・有限 burn の実測結果 |
| terminal descent | state、姿勢、engine readiness | 事前承認済み自動降下手順 | 接地・残存・回収それぞれの証拠 |

古い観測や通信断を「正常」と補完しない。飛行は判断待ちの間も進むため、汎用の
`hold` を「空中停止」として実装しない。予定した安全側の継続・帰還動作を事前に定義し、
approval scope、期限、観測の鮮度、Rules verdict を action に結び付ける。
承認時点から条件が変わった場合の再評価も記録する。

LLM judges. Human approves. Rules constrain. Executor acts. Verifier checks. Repair loops.

`mission_completed` は一つの接地 flag から求めない。契約には payload delivery、
satellite checkout/service、booster disposition、Ship disposition を独立に指定する。
意図した booster expending が含まれる Flight 14 参照実験に booster catch を要求しない。
一方、「衛星運用開始と両段再使用」という将来シナリオは、Flight 14 の実績再現とは別にする。

## 較正と検証の順序

1. **データ凍結**: 世代・flight・出典・予定/報告/映像観測・time basis・読み取り誤差を保存。
   未確認の actual times は null とし、予定表から埋めない。
2. **観測点の抽出**: launch、staging、SECO、burn、deployment、entry、flip、contact の
   timecode と画面表示を少数ずつ確認。欠測・凍結表示・編集カットは flag を付ける。
3. **数値の独立検証**: NASA check cases を参考に地球回転、座標変換、自由落下、軌道、
   推進剤消費、姿勢運動を検査。既存 Basilisk 二体比較はその限定された比較として保持する。
4. **較正**: 上昇や entry の一部でパラメータ範囲を推定し、使わなかった区間で検証する。
   同じ観測点へ fitting した一致だけを予測精度と呼ばない。別世代の飛行を同一機体の holdout にしない。
5. **制約付き E2E**: `recorded_flight14` と異常がなかった反実仮想 `counterfactual_nominal` を区別する。
   さらに vacuum engine out、orbit no go、dispenser jam、missing receipt、
   communication loss、landing relight failure を、同一の固定 profile で実行する。
6. **精度の公表**: phase ごとの time/altitude/speed/ground-track/attitude 残差と不確かさを示す。
   映像分解能より小さい合格閾値を置かない。閾値は結果を見る前に凍結し、未測定項目は未検証とする。

新しい outcome を見てから質量や空力を調整した場合は別 run として保存する。修正前の失敗を
消さず、未使用条件での再検証を要する。LLM の added value を後で測る場合は、まず simple
comparator と oracle の差、候補行動が実際の終端結果を変える余地を確認する。

## 実装順と今回の到達点

最優先は **Flight 14 の health gate と早期帰還を含む phase model**、および観測と積分結果を
並べる replay である。次に回転地球・有限燃焼・空力降下を持つ 3D model、最後に 6 DOF と
衛星の長期 checkout/軌道上昇を追加する。これは実装順であり、完了したとの報告ではない。

今回行ったのは公開一次資料の調査、既存コードとの差分分析、次のモデルの仕様化。
新しい 6 DOF run、実機 telemetry の ingest、26 基の運用確認、Gateway からの実行は行っていない。

予定時刻、運航者報告、未確定入力は [研究用データ](starship-flight14-research.json) にも保存した。
この JSON は runtime configuration ではなく、現行 simulator が読み込むものではない。
