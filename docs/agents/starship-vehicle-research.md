# Starshipの機体・推進・再突入モデルの調査メモ

調査基準日: 2026-10-03。公開一次資料の本文を確認した研究メモであり、SpaceXの飛行ソフトウェア、機体性能表、飛行テレメトリの代替ではない。実装済みモデルの検証結果は [6DOFモデルの契約](starship-sixdof-contract.md) を参照する。

## 先に固定する結論

精密化では特定の飛行と各段の構成を固定する。`Starship V3` という名前だけで、過去の設計目標、現在の紹介ページ、環境評価の最大構成を合成してはいけない。帰還は、軌道離脱、姿勢制御を伴う大気飛行、終末の反転と着水噴射に分ける。現行の燃料制約付き軌道追従モデルの失敗を、実機Starshipの帰還不能と解釈してはならない。

以下の `reported` は事業者が公表した内容、`planned` は計画、`assessment_envelope` は環境評価で扱った構成、`unknown_in_reviewed_sources` は今回の資料群から値を確定できなかった事項を表す。事業者発表は独立した生テレメトリ検証とは区別する。

## 一次資料と確認範囲

| ID | 日付・資料・位置 | 本文で確認した用途 |
|---|---|---|
| SX-V3 | 2026-05-12 [Introducing Starship V3](https://www.spacex.com/updates#starship-v3), Super Heavy / Starship / Raptor 3 Change Highlights | V3の構成差分と公表エンジン値 |
| SX-SPEC | 更新日記載なし、2026-10-03閲覧 [Starship](https://www.spacex.com/vehicles/starship), Overview / Starship / Super Heavy / Raptor Engines | 現行紹介ページの代表寸法・容量。飛行番号に結び付けない |
| SX-F7 | 2025-01-16 [Seventh Flight Test](https://www.spacex.com/launches/starship-flight-7), 飛行後本文 | 更新上段の飛行、エンジン再点火の個別成否 |
| SX-F11 | 2025-10-13 [Eleventh Flight Test](https://www.spacex.com/launches/starship-flight-11), 冒頭、帰還段落、Flight Test Timeline | 各段の世代、空力降下、実績と概略計画時刻の区別 |
| SX-ORB | 2026-09-15 [Starship to Orbit](https://www.spacex.com/updates#orbital-starship), Initial orbit design | 初期軌道ミッションの健康確認と再点火の計画構造 |
| FAA-25 | 2025-04 [Final Tiered EA Executive Summary](https://www.faa.gov/media/94341), 印刷p.5 / PDF p.6, Table ES.2; 印刷p.6 | 評価用機体構成、残留推進剤、着水の分類 |
| FAA-26 | 2026-02 [Additional Launch Trajectories and Starship Boca Chica Landings Final Tiered EA](https://www.faa.gov/space/stakeholder_engagement/spacex_starship/Final_Tiered_EA_Additional_Launch_Trajectories_Starship_RTLS_mission_profiles_SpaceX_Starship-Super_Heavy_Boca_Chica.pdf), §2.2.1-2.2.2, Figure 1-2 | 概念的な発射・帰還経路と航空危険区域。実測航跡ではない |
| NASA-HS | 2024-11-16更新 [Starship Hot Staging - From Concept to Flight](https://www.nas.nasa.gov/SC24/research/project24.php), 本文・Quick Facts | 段間噴流の物理と高忠実度解析の境界 |
| NASA-TPS | 2025-06-18 [NASA Perspective on the Future of TPS for Hypersonic Flight](https://ntrs.nasa.gov/api/citations/20250006125/downloads/NASA_TPS_Perspectives_ReusableRocketsWorkshop_RevB.pdf), slides 2, 5-6, 13, 16 | 熱環境と材料応答を分ける必要性。NASA所属研究者の講演であり、slide 2はNASA公式見解ではないと明記 |

SpaceXのJavaScriptページはブラウザーの表示本文で確認した。FAA-25の表とNASA-TPS slide 16はPDFを画像化して列・図を確認した。検索結果の抜粋だけを根拠に数値を採用していない。2020年版Users Guideの旧URLは今回404であり、検索索引に残った内容を現行仕様へ取り込んでいない。

## 公表値と世代差

SX-SPECの代表値は次の通り。すべて `reported` であり、実際の搭載量・推力履歴ではない。

| 項目 | Ship | Super Heavy | 全体・注記 |
|---|---:|---:|---|
| 高さ | 52 m | 72 m | 124 m |
| 直径 | 9 m | 9 m | 同径 |
| 推進剤容量 | 1,600 t | 3,650 t | 満載実績や使用可能量とは異なる |
| 紹介ページの推力 | 1,614 tf | 8,240 tf | 圧力・運転条件の詳細は未記載 |
| エンジン | sea-level 3 + RVac 3 | 33 | 内側13基が可動、外周20基 |
| 公表の軌道輸送能力 | - | - | 完全再使用構成で100 t超という設計能力 |
| 推進剤種別 | - | 過冷却メタン・液体酸素 | 機体紹介の記述 |

SX-V3は、Raptor sea-levelを230→250 tf、vacuumを258→275 tf、sea-levelエンジン質量を1,630→1,525 kgと公表している。boosterは4→3枚のgrid fin（各50%大型化）、投棄式から統合型hot stageへ移行。Shipは後部flapの駆動、RCS、長時間coast用の低温推進剤管理、PEZ dispenserを更新。これらは機体全体の乾燥質量・Isp・制御則を与えない。

SX-F11は「第2世代Shipと第1世代Super Heavyの最後の飛行」と明示する。したがってShipとboosterの世代フィールドは別に持つ。SX-F7は更新された上段が飛行した時期の参照例だが、現行の飛行後本文だけからV2の全寸法やpropellant volume増分を確定しない。V1/V2のフラップ配置や機体寸法は、使用する飛行の画像・その時点の公式資料に結び付けて追加する。

FAA-25の評価値は、旧構成がShip/Super Heavyの順に50/71 m、6/37 engines、1,500/3,700 t、新構成が70/80 m、9/35 engines、2,650/4,100 t、28.7/103 MN。いずれも `assessment_envelope` であり、V3の33+6基という構成と混ぜない。同資料のShip最大約101 t残留推進剤はdownrange着水に関する評価値であり、再突入開始時に100 tを自由に燃やせるという仕様ではない。

### 単位と数値整合性

設計上の推奨: 元資料の単位と換算値の両方を残す。`tf`を質量`tonne`として扱わない。1 tf = 9,806.65 Nなので、250 tf = 2.4516625 MN、275 tf = 2.69682875 MN。ただし公表丸め値から換算した桁数は精度を増やさない。代表推力の単純合計と別ページの機体推力を完全一致させるために値を補正しない。大気圧、throttle、構成、丸めの不明点を先に解消する。

## 飛行中に必要な状態

公開事実: SX-F11の帰還本文はbanking、4枚のflapによる誘導、終末のflip、landing burn、soft splashdownを報告している。掲載timelineは `All Times Approximate` とされ、計画・説明用の時刻を実測イベント時刻として採用できない。SX-F7ではboostback時に点火しなかったエンジンがlanding burnで点火しており、`engine_failed` の永久ラッチだけでは実績を表現できない。

設計上の推奨:

| サブモデル | 最低限持つ状態 | 検証すべき観測 |
|---|---|---|
| 推進 | エンジン別状態、起動試行、throttle、推力立上り、gimbal、燃焼ごとの使用量 | 公表された点火本数・停止・再点火成否 |
| 液体・ガス | メイン/ヘッダーの区別、残量、利用可能量、圧力、温度、coast経過時間 | 再点火前提、低圧/漏れ/供給不能の故障ケース |
| 姿勢 | quaternion、角速度、慣性テンソル、重心、flap角・速度、RCS残量 | bank、flip所要時間、姿勢保持・喪失 |
| 空力 | Mach、迎角、横滑り、flap依存の力・モーメント係数 | 大気降下の速度/高度、横方向の位置、姿勢 |
| 熱 | windward/flap/hinge/engine-bayなどの区画、熱流束、熱量、材料温度 | 区画ごとの損傷・温度。光学的発光だけで温度を決めない |
| 着水・回収 | 接触速度、姿勢、角速度、位置、その後の機体状態 | 接触、浮揚、沈没、回収、再使用を別々に記録 |

上記はMissionOS側の設計提案であり、SpaceXの実際の内部状態機械を再現したとの主張ではない。

## 再突入・TPSの精度境界

NASA-HSはhot stagingにおける上段点火とbooster継続燃焼、段間の噴流衝突を説明している。同研究はLoci/CHEMによる約5億cell、約4千万CPU時間の解析を行い、IFT2データと比較した。一般剛体シミュレーターにステージ分離イベントを追加するだけでは、この噴流荷重を検証したことにならない。

NASA-TPS slide 13は高温での材料特性、環境境界条件、試験との比較を必要としている。slide 16は段差・隙間・突起・制御面が局所流れや乱流遷移、加熱を変えると説明する。よってMissionOSの単一Sutton-Graves熱流束と累積熱量の閾値は、機体の熱的生存を判定する証拠にはしない。

設計上の推奨: 最初の精密化は低次元空力・熱モデルと不確かさの範囲を明示したものとする。CFDや材料試験の校正データがない段階で、詳細な3D外観を「高忠実度物理」の代わりにしない。flap喪失や局所熱保護の故障を注入できても、その故障確率や実機寿命を予測したとは扱わない。

## 今回の公開資料では確定できない量

次の値は `unknown_in_reviewed_sources` とする。公開されていないことを網羅的に証明したという意味ではない。

- 対象飛行の乾燥質量、慣性テンソル、重心移動、未使用残留量と着地用reserveの配分。
- RaptorのIspマップ、最小throttle、gimbal角/角速度制約、再起動の全条件。
- 各世代の全Mach/迎角/flap角領域の空力係数と局所圧力・熱流束データ。
- 対象機体のtile材質・厚さ・接合条件ごとの温度依存物性、損傷許容値。
- 実機のnavigation誤差モデル、故障検出閾値、flight softwareの制御則。
- 公開映像中の速度・高度の座標系、丸め・時刻遅延、内部フィルター処理の詳細。

仮定する値は `assumption` として、採用理由、範囲、感度、結果への影響を記録する。既存モデルの120 t dry mass、100 t return propellant、Isp 330 s、Cd 1.2、面積200 m²を公式のStarship値へ昇格させない。

## ミッションと地理の扱い

SX-ORBの初期軌道計画は、まず受動的に再突入する上昇軌道に入り、健康確認後にsea-level Raptor一基で軌道投入、最終的にも一基で離脱する。これは計画構造であり、各フライトが全段階を達成したかは個別の飛行後記録に結び付ける。

FAA-26 §2.2.1-2.2.2とFigure 1-2は低傾斜の概念的な経路と航空危険区域を評価する。危険区域は機体の中心航跡でも着水誤差楕円でもない。FAA-25はhard water landing、減速着水後の転倒等、飛行中破壊を別の状態として扱う。接触速度だけで回収や再使用を判定しない。

設計上の推奨: 3D Earth-fixed位置と慣性系の速度を明示し、地球自転を含める。MissionOSの帰還条件には着水対象領域、時刻範囲、姿勢・角速度、最終受信時刻、飛行後状態を含める。衛星放出の完了と、衛星の軌道・初期通信・運用移行は別のVerifier結果にする。

## 実装への採用順序

1. 飛行番号、Ship構成、Super Heavy構成、pad、planned/observedを凍結したシナリオにする。
2. 3D位置・速度、地球自転、層別大気、可変質量、有限推力を入れ、まず高度・速度・主要イベントを検証する。
3. 空力降下と終末燃焼を別モードにし、迎角/bankを持つ3DOFモデルを作る。次に姿勢・flap・gimbal・慣性を含む6DOFへ進める。
4. 公表された失敗を成功へ書き換えず、健康確認、代替着水、deployment中断、回収中断のミッション判断を検証する。
5. 公開観測への適合と、未知条件での予測を区別する。校正に使った飛行とは別の飛行で検証する。

各値の契約には `source_url`, `source_date`, `source_locator`, `vehicle_scope`, `classification`, `units`, `uncertainty`, `retrieved_at` を必須とする。観測データには映像時刻とmission elapsed timeを別々に保存し、補間点は実測点として数えない。物理モデルの追加だけでLLM判断の価値を主張せず、承認・Rules・実行・観測・検証の責任分離を維持する。
