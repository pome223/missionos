# 打ち上げから連続した Super Heavy 帰還・キャッチの契約

調査日: 2026-10-04。対象は公開された V3 の機体構成を参照する、ローカルな工学シナリオである。この文書は要求と出典を定める。実行結果、到達、連続支持、実機再現の成功証明ではない。

既存の [終端初期化キャッチ](starship-booster-catch.md) は、タワー付近に与えた状態から接触を積分する試験である。ここでは発射、段分離、boostback、空力降下、着陸燃焼を経て得られた**同一の物理状態**を、その接触計算へ渡す。届かなかった試行も残す。

## 一次資料で確定する範囲

| 資料 | 確認した事実 | 実装への制約 |
| --- | --- | --- |
| [SpaceX Flight 5](https://www.spacex.com/launches/starship-flight-5) | 上昇、分離、boostback、coast、landing burnの後にアームで捕捉。多数の機体・地上条件が前提 | 地点への到着と、キャッチ許可・機械的支持を別々に検証する |
| [SpaceX Flight 6](https://www.spacex.com/launches/starship-flight-6) | boostback中のタワー健全性チェックで中止し、事前計画した退避と海上着水を実行 | 未健全・期限切れ・許可なしでタワーへ接触させない。退避は実際のcommandと後続運動として記録する |
| [SpaceX Flight 13](https://www.spacex.com/launches/starship-flight-13) | V3のboostback高推力区間は33基で実施 | 旧世代の13基boostbackをV3の普遍的上限にしない |
| [SpaceX Flight 14](https://www.spacex.com/launches/starship-flight-14) | boostbackは計画33基中31基。landingは計画13基中11基から5基、3基へ移行。boostbackで主タンクの残存LOXを使い切る試験を行った後、海上へ降下 | V3参照の着陸燃焼は高推力・精密誘導・終端の段階を表現する。各切替閾値は未公開。Flight 14を実キャッチの記録と呼ばない |
| [SpaceX V3紹介・2026-05-12](https://www.spacex.com/updates#starship-v3) | 3枚の大型grid fin、新しいcatch point、配置・高さの変更。タワーアーム短縮と電動化 | 旧世代の4枚finやcatch pin寸法を混ぜない。現在の2支持点は依然として代理形状でありV3 CADではない |
| [SpaceX Flight 8報告・2025-05-22](https://www.spacex.com/updates) | 分離時に中央3基を残し、着陸燃焼の最後にも中央3基でアームへ誘導。再点火失敗の一部は後続燃焼で回復 | 燃焼別の始動成否を区別する。3基の有限推力による姿勢変更を、外部の任意トルクで置換しない |

Flight 5/6/14は調査時に描画された本文を確認し、Flight 13は一次ページの検索索引本文を確認した。予定timelineと実飛行後記録を区別する。Flight 14の予定時刻をキャッチ経路の強制イベントや観測済み閾値にしない。Flight 5/6の公開事実から、タワーの全判定項目・閾値・手動GOの締切時刻は復元できない。

## 誘導手法の公開根拠

**SpaceX固有の誘導則は未公開。** 次は実装する汎用手法の根拠であり、Super Heavyのflight softwareを取得したという主張ではない。

- [Blackmore, Autonomous Precision Landing of Space Rockets, National Academies](https://nap.nationalacademies.org/skim.php?chap=33-42&record_id=23659): Falcon 9の帰還で、boostback目標、finによる大気中制御、終端燃焼による残差修正を分ける。終端で初めて横方向誤差を修正する設計にはしない。Falcon 9の実績からSuper Heavyの係数・engine sequenceは転用しない。
- [NASA, Application of Direct Force Control to Human-Scale Mars EDL](https://ntrs.nasa.gov/api/citations/20200005410/downloads/20200005410.pdf), pp.4–8: 数値予測・修正による経路目標、迎角・横滑り・燃焼開始・推力の更新を6DOF環境で評価する。3DOFの内部予測は、6DOFでの追従成功とは別。地球大気やSuper Heavyの係数源ではない。
- [NASA, SCvx with Time-Varying Mass Properties](https://ntrs.nasa.gov/citations/20230017074), [説明資料](https://ntrs.nasa.gov/api/citations/20230018515/downloads/SciTech2024_Charts_updated.pdf), pp.3–9: 姿勢と並進の結合、推力・傾斜・速度・進入経路の制約を含める。trust regionやvirtual controlを用いた計画が返っても、残差と実ダイナミクスで成立を確認する。仮想入力を実機への力として適用しない。
- [NASA SCvx実装チュートリアル](https://ntrs.nasa.gov/citations/20230009811): 3DOFの数理構成・離散化の参考。これ単独を6DOFキャッチ制御の実証と呼ばない。
- [NASA/JPL minimum landing error guidance](https://ntrs.nasa.gov/citations/20120001230): 指定地点へ到達不能なら、その不能を残して別の目的を扱う。一般の制約付き問題に、同論文の最適性・収束保証を無条件で拡張しない。
- [NASA SPLICE関連6DOF誘導の実装評価](https://ntrs.nasa.gov/citations/20210024113): 誘導計算の時間制限と内側の追従制御を区別する。計画の再計算失敗・時間超過も結果であり、現在状態を計画上の状態へ置換しない。

この段階で採用する予測・修正器は、初期状態と仮定したモデルから未来状態を**予測**するものである。予測は観測でも実行でもない。名前だけをSCvx/MPCへ変えず、実装した探索変数、目的関数、制約、計算回数、収束・未収束を記録する。

## 現状モデルから優先して直す点

1. **終端目標をアームの支持点で定義する。** 地表の緯度経度だけでは不十分。回転するタワー座標系で支持点の位置、相対速度、姿勢、角速度、残量、エンジンの実スプール状態まで目標集合に含める。支持点の高さをCG高度へ読み替えるときは、現在のCGと姿勢を使う。
2. **boostback・coastで終端到達性を見積もる。** 現行の一定時間割り算による水平速度目標や、coast中に常に鉛直姿勢を要求する設定は汎用仮定である。風・抗力・姿勢による進路変化と、燃焼開始時に残る横速度・燃料を予測し、上流で目標を更新する。
3. **高推力から終端推力へ移行する。** `fixed_v1`の最大3基だけのlanding burnはV3の公表系列を表現しない。13/5/3の段階を参考にしても、閾値・throttle・再始動制限は設定した仮定として残す。支持点の減速に必要な推力と、機体を向けるための実現可能トルクを同時に評価する。
4. **燃料の使用可能性を区別する。** 現在の単一reservoirと60 t reserveは実機のmain/header/feed pathではない。主タンクLOXを使い切ったFlight 14に同じ燃料変数をそのまま適用すると、後続燃焼を表現できない。単一reservoirを維持する段階では、全燃料のうち誘導が使用可能な予算を分けた近似と明記し、実タンク量やLOX枯渇再現を主張しない。
5. **grid finは力・トルクとして効かせる。** 公開されている3枚構成を保つ。現在の等間隔・同面積・solid plate近似、係数、位置、上限角は実機同定値ではない。形状変更で成功するように調整した結果を、実際のfin性能の再現としない。

乾燥質量250 t、分離残量260 t、着陸reserve60 t、Isp330 s、最小throttle、8度gimbal、姿勢PDゲイン、空力係数、engine位置、catch point寸法・剛性・許容荷重などは、現在の実行仮定である。公開値と区別した設定をhash固定する。失敗を消すためにrunの途中で上書きしない。

## 連続実行と境界状態

発射→分離→帰還→接触の各境界で、次を同一時刻の状態として保存する。

```text
time_s, r_eci_m, v_eci_mps, q_body_to_eci, omega_body_rad_s,
propellant_kg, every engine_state, every flap_angle_rad
```

エンジン状態にはthrottle、gimbal、availabilityを含む。phase名の変更やrecord schemaの変換は可能だが、物理値の修正・座標への吸着・速度ゼロ化・姿勢の即時一致・燃料補充・故障消去は不可。quaternionの符号同値だけで、別の回転まで許容しない。

帰還結果が終端集合に達したときだけ、**その実状態**を接触計算に渡せる。タワー付近の別fixtureを生成して接続したことにしない。未到達、機体接地、燃料不足、計算上限、健全性拒否、期限切れは、到達未達または退避の結果である。

予測器が作ったtrajectoryと実際の軌跡を別に保存する。保存した制御時刻では、現在観測、予測条件、選択した目標・phase、要求command、実actuator状態、後続の保存状態を対応付ける。checkpoints は約0.5秒間隔であり、全積分stepではない。独立検証は保存状態の連続性・有限actuator・到着条件を調べるもので、積分器の再実行ではない。未来の故障系列や真の回復時刻を誘導に渡さない。

## 保存recordと終端集合

`recovery_record.schema = missionos.starship_booster_recovery.v1` は次を含む。

- `input_separation_state`: 親の分離状態の完全な写し。
- `guidance_configuration`: `predictive_return_v1`の開発仮定。60度のbraking tilt上限、最大24回のcoast予測などはSpaceXの値ではない。
- `checkpoints`: `time_s`, `phase`, `state`, `command`, `com_rate_body_mps`, `navigation`。`state`には全engine/flapの実状態を入れる。
- `boostback_plan`: command-spaceの候補と選択。`prediction_is_execution=false`。再計画時には別eventを保存し、最後の計画へ結び付ける。
- `cutoff_predictions`: 完全な6DOFによる打切り候補の予測記録。最大24件で、時刻・件数を結び付ける。実状態と姿勢参照を複製し、同じpowered-settle→coast→landingの処理を使う。`remaining_duration_s`は実行に残された時間を引き継ぎ、新たな1200秒を付け足さない。これも実際に選ばれた軌跡とは別の反実仮想である。
- `handoff`: `eligible`, `time_s`, `state`, `observation`, `limits`。未到達時の`state`はnullで、到達したようなfixtureを生成しない。
- `requested_duration_s`, `resolved_duration_s`: 指定と実際の時間予算を分ける。最大1200秒。
- `forecast_only`: 実行記録は明示的にfalse。内部で同じ6DOFを走らせた予測はtrueとし、実行の代用として検証を通さない。

到達条件は、回転するタワーのENUで両支持点を再構成して調べる。支持点の材料速度はCG速度だけではなく、`v_CG + R(omega × (pin − CoM) − CoM_rate)`から地球回転速度を引いた値である。燃焼中のCG移動を省かない。

現在の代理設定では、支持点中点の水平誤差が0.2 m以下、両点の面上高さが3–3.8 m、下降速度が−3〜−0.5 m/s、各水平速度が0.4 m/s以下、機体角速度が0.01 rad/s以下である。傾きとbody Xの東方向からの角度はともに`atan(0.4 m / 最大支持点レバー長)`以下。これらはcatch設定から導く開発上の集合であり、実タワーの許容差ではない。

最低残量は、現在質量でhover推力を出す近似流量に、最も遅い許容接近時間と4倍のthrottle時定数を掛けて求める。`reserve_horizon = 3.8 m / 0.5 m/s + 4 × throttle_tau`、`reserve = mass × 9.81 / (Isp × 9.80665) × reserve_horizon`である。終端fixtureの初期燃料30 tや、boostback打切り予算60 tとは別の量である。

`verify_recovery`は終端集合と完全な状態handoffを独立に再計算し、その後`verify_catch`へ接触記録を渡す。返す`handoff_reached`と`catch_supported_after_handoff`は別の事実である。到達しても接触・支持に失敗すれば後者はfalseになる。この検証器単体の`launch_connected`は常にfalseであり、親の打ち上げ検証まで通った場合だけ外側の`launch_connected_catch_supported`を立てられる。

接触側の`control_policy`は`fixed_v1`または`net_thrust_trim_v1`を明示する。後者は、観測したエンジン推力のbody方向を目標姿勢の要求に補償する開発制御であり、実姿勢・速度・荷重を書き換えない。全打ち上げの親契約は後者と最大30秒を固定する。独立した従来の終端fixtureは`fixed_v1`・12秒を既定として保つ。接触後はエンジンとRCSを停止要求し、有限減衰の後に機械的支持だけで条件を満たしたかを検証する。

保存checkpoint間では、燃料の増加、actuator速度上限を超える変化、故障availabilityの無記録変更、速度と整合しない位置の跳躍、角速度と整合しない姿勢の跳躍を拒否する。疎な記録から全外力・全中間状態を一意に復元したという意味ではない。

entryの実行側候補は相対角60度まで、安価なpoint-model予測は30度までとする。これも汎用の開発制御であり、実機の迎角運用を主張しない。実行側は静的trim予測で、設定したfin角上限と残余RCSトルク内に収まる候補だけを使う。trimの動圧検査は予測中の現在動圧とprofileの`max_q_pa`の大きい方で行い、薄い大気で成立した姿勢をそのまま将来の高動圧でも成立すると扱わない。実際の空気力は現在の密度・速度で積分する。独立検査は二つの動圧、残余トルクの比例関係と有限範囲、予測角度、`trim_is_executed_deflection=false`を確認する。trim solverの再実行や、候補へ有限時間で追従できる証明ではなく、実flap状態の遅れ・速度上限は別に保ったまま積分する。

## 権限と検証

```text
LLM proposes mission scope → human approves local simulation → Rules constrain
→ deterministic guidance/executor integrates → independent verifier checks
```

MissionOSのGOは、このローカル試行の範囲である。実SpaceXのFlight Director承認、FAA許可、実機制御、実キャッチを意味しない。タワー健全性の公開上の存在を、今回のセンサー閾値の実機正しさと混同しない。

独立verifierはproducerの誘導関数を呼んで同じ答えを出すだけにしない。少なくとも、固定した入力、分離継承、記録されたphase/command、時刻と有限状態、終端集合への実到達、handoff完全一致、後続接触の独立検査を行う。継承検査だけで全区間の数値積分を独立再現したと主張しない。

表示と結果は次を別々に扱う。

| 事実 | 必要な証拠 |
| --- | --- |
| 発射からの連続実行 | 親runの発射・分離記録と、同一状態からの帰還記録 |
| タワー終端への到達 | 積分された位置・速度・姿勢・角速度・残量が設定した集合に入る |
| 接触計算へのhandoff | 帰還末尾とcatch開始の全物理状態が一致する |
| `simulated_catch_supported` | 両支持点で有限荷重・有限stroke・速度等の制約を満たす連続支持 |
| 実機キャッチ・構造健全性 | このモデルでは証拠なし。`catch_verified=false`, `physical_execution=false` |

nominalだけでなく、タワー未健全、終端集合の外側、操舵・推力不足、燃料不足、片側支持、handoff改ざんを試す。支持成立でも、代理形状・接触係数の下でのローカル結果である。全打ち上げが失敗する場合に終端fixtureの成功を合算して、全体の成功へ昇格させない。

## 証拠の固定

実行には元studyのhash、profileとcatch設定の内容・hash、source hashes、選択policy、要求時間上限、実積分刻み、観測/command/event、未到達理由を残す。source変更前後が一致しない試行は、修正後の証拠として使わない。失敗を上書きせず、数値を調整した候補と評価用候補を区別する。

公開文書に実行済みの数字を追加する場合は、その具体的なartifactと版を確認してから追記する。調査・設計・コード・試験・実機検証を別の段階として保つ。

## 実装した入口と実行上限

既存の[MissionOSチャット起動手順](../examples/starship-mission-chat.md)でGatewayへ接続し、次の順に操作する。

```text
Starship 打ち上げからキャッチ
/approve
/run
/status
```

`Starship sixdof_launch_catch`も同じcatalog項目を選ぶ。計画時には`scenario=sixdof_launch_catch`、`booster_recovery.policy_id=predictive_return_v1`、固定profile/source hashes、実行上限を表示する。`/approve`はその計画に対して専用scope `local_launch_connected_booster_catch_simulation`を付与し、`/run`が別プロセスの実行を開始する。従来の近傍初期化scopeを流用しない。

この経路のworker wall time上限は900秒である。帰還計算の最大1200シミュレーション秒、完全なcoast予測の最大24回、到達後の接触計算最大30シミュレーション秒とは別の時計・上限である。接触のpolicyは`net_thrust_trim_v1`へ固定する。LLM/Jevへ誘導・点火・タワー指令の権限を追加しない。

帰還予測のCPU計算中はシミュレーション時刻を進めない。これはオフラインの誘導開発であり、900秒のjob上限や2モデル秒ごとの再予測は、実時間の飛行ソフトとしての計算遅延を保証しない。既存のMissionOS監督経路で、提案待ちの間も物理時刻を進める契約とは別である。

開発用のCLI入口は次の形である。非空の出力先は拒否し、以前の失敗を上書きしない。このコマンド例を掲載したこと自体は、全飛行の実行済み証拠ではない。

```sh
python scripts/run_starship_sixdof.py --approve-simulation \
  --scenario launch --booster-policy predictive_return_v1 \
  --output-dir output/starship-launch-catch-example
```

内部APIは`simulate_recovery(profile, separation_state, catch_config, duration_s=...)`と、独立した`verify_recovery(booster_run, separation_state, profile, catch_config, catch_run=...)`に分かれる。前者は到達条件を満たす実状態を返すだけである。親のmission executorが、その状態を`simulate_catch(..., initial_state=handoff_state, duration_s=30, control_policy="net_thrust_trim_v1")`へ渡す。未到達ならcatchを起動しない。`forecast_only=true`の内部予測を実行記録として渡すことは拒否する。
