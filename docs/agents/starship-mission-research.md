# Starship 実飛行の根拠とミッション境界

調査基準日: 2026-10-03。SpaceX の公式ミッションページをブラウザーで描画し、
飛行後本文とタイムラインを確認した。検索結果の抜粋だけを実績認定に用いていない。
以下の「実績」は SpaceX の飛行後報告に基づく `operator_reported` であり、
MissionOS が生テレメトリーを独立検証したという意味ではない。

## ミッション世代と実績

各行の一次資料リンクは飛行後本文を指す。日付は打ち上げ日、CT はページ記載の
米国中部時間。時刻がない行に推測の打ち上げ時刻を補わない。

| 飛行・日付 | ペイロード／上段の実績 | ブースターと帰還の実績 | 根拠 |
| --- | --- | --- | --- |
| Flight 5 / 2024-10-13 | Ship は宇宙から再突入、flip と着水を実施。実飛行終了は T+01:05:40 と本文に明記 | Super Heavy を塔のアームで初 catch。Ship はインド洋着水であり、Ship の catch ではない | [F5 飛行後本文](https://www.spacex.com/launches/starship-flight-5) |
| Flight 6 / 2024-11-19 | Ship が宇宙空間で単一 Raptor を再点火し、その後インド洋に soft splashdown | 塔の重要機器の自動健全性チェックが catch を中止。事前設定の diversion と着水に移行 | [F6 飛行後本文](https://www.spacex.com/launches/starship-flight-6) |
| Flight 8 / 2025-03-06 17:30 CT | Ship の上昇燃焼完了前に後部で異常、複数エンジンと姿勢制御を喪失。最終通信は約 T+09:30 | Super Heavy の3回目の catch。boostback は予定13基中11基、着地燃焼開始は13基中12基を再点火 | [F8 飛行後本文](https://www.spacex.com/launches/starship-flight-8) |
| Flight 9 / 2025-05-27 18:36 CT | 8基の simulator を予定したが扉が開かず放出できない。姿勢異常により再点火を省略。圧抜き後、約 T+46分で通信喪失 | 初の Super Heavy 再飛行。着地燃焼の直後に機体喪失 | [F9 飛行後本文](https://www.spacex.com/launches/starship-flight-9) |
| Flight 10 / 2025-08-26 18:30 CT | suborbital 軌道で8基の Starlink simulator を放出。宇宙での Raptor 再点火、4枚のフラップによる降下、flip、インド洋 soft splashdown | 着地燃焼で中央1基を意図的に無効化し、中間リングの予備エンジンを使用。水面上 hover 後に着水 | [F10 飛行後本文](https://www.spacex.com/launches/starship-flight-10) |
| Flight 11 / 2025-10-13 18:23 CT | 8基の simulator を放出、宇宙での再点火、banking、インド洋 soft splashdown | boostback は13基中12基、着地燃焼は予定13基を再点火。次世代向け燃焼方式を試験し、hover 後に着水。第2世代 Ship／第1世代 booster／当時の Pad 1 構成の最終飛行 | [F11 飛行後本文](https://www.spacex.com/launches/starship-flight-11) |
| Flight 12 / 2026-05-22 17:30 CT | 20基の simulator と、Ship 撮影用の改修 Starlink 2基を suborbital 軌道へ放出。真空 Raptor 1基停止後も所定軌道へ。最後は2基で着水 | V3、Raptor 3、Pad 2 の初飛行。上昇中1基停止、boostback は不完全で早期終了、booster は hard splashdown | [F12 飛行後本文](https://www.spacex.com/launches/starship-flight-12) |
| Flight 13 / 2026-07-24 17:51 CT | 実物の Starlink V3 20基を放出。全基と RF／レーザーで通信したが suborbital であり、放出約20分後に大気圏で消失する見込みと報告。Ship は再点火、banking、soft splashdown 後、海上で機体を保持 | booster の高推力 boostback は33基で実施したが早期終了、着地再点火は一部のみで hard splashdown | [F13 飛行後本文](https://www.spacex.com/launches/starship-flight-13) |
| Flight 14 / 2026-09-28 07:48 CT | 初の軌道投入と26基の V3 放出を報告。全基と接続したが、checkout と軌道上昇は顧客サービス開始前の後続工程。上昇時のエンジン異常を受け早期に北太平洋へ帰還 | Ship は標的位置に着水。本文は catch・陸上回収・再使用の達成を示さない | [F14 飛行後本文](https://www.spacex.com/launches/starship-flight-14) |

F13 の「消失する見込み」は実際の全衛星の消失を独立追跡した証拠ではない。
F13 の海上で intact という記述も港への回収完了、整備完了、再使用の証拠ではない。
F12 ページには数値0の `ON-ORBIT / VEHICLE TRACKER` UI が残っていたが、
本文は suborbital と明記する。UI の汎用ラベルを軌道投入の証拠にしてはならない。

## 公開された予定時刻と実績時刻

次表は各公式ページの `FLIGHT TEST TIMELINE` にある概略予定値である。
映像から読み取った実績時刻でも、テレメトリー実測値でもない。
各ページには `All Times Approximate` とある。単位は打ち上げ後 mm:ss。

| 予定イベント | F10 | F11 | F12 | F13 |
| --- | --- | --- | --- | --- |
| hot staging | 02:38 | 02:39 | 02:24 | 02:21 |
| Ship エンジン停止 | 08:57 | 08:58 | 08:11 | 08:05 |
| 放出開始 | 18:27 | 18:28 | 17:37 | 16:40 |
| 放出終了 | 25:32 | 25:33 | 27:15 | 27:39 |
| 宇宙での再点火実証 | 37:48 | 37:49 | 38:37 | 38:58 |
| 再突入 | 47:29 | 47:43 | 47:47 | 47:30 |
| 最終 landing イベント | 66:30 | 66:25 | 65:26 | 65:21 |

出典: 上表の [F10](https://www.spacex.com/launches/starship-flight-10)、
[F11](https://www.spacex.com/launches/starship-flight-11)、
[F12](https://www.spacex.com/launches/starship-flight-12)、
[F13](https://www.spacex.com/launches/starship-flight-13) の各タイムライン。

特に F12 では予定表が3→2→1基の着地燃焼を示す一方、飛行後本文は2基で着水したと
報告している。予定表を再生するだけでは実飛行の再現にならない。F14 でも飛行後本文は
早期帰還と記すのに、予定表には約9時間50分の終端が残る。実績の終端時刻として採用しない。

## 故障を一種類にまとめない

以下は SpaceX の事故調査における「最も可能性の高い原因」の報告であり、
詳細な故障確率や閾値を推定するためのデータセットではない。

| 調査記事・掲載日 | 報告された原因／対策 | モデルへの反映案（MissionOS の設計判断） |
| --- | --- | --- |
| `NEW YEAR. NEW SHIP. NEW LESSONS.` / 2025-02-24 | F7 は強い振動応答による推進系への荷重、漏洩、後部火災。配管・推力条件・換気系を変更 | 振動／漏洩／火災を別状態にし、単なる推力低下で済ませない |
| `FLY. LEARN. REPEAT.` / 2025-05-22 | F8 は中央 Raptor のハードウェア故障による推進剤混合・着火。F7 と原因は異なる。booster の再点火不良は点火器近傍の熱条件が有力 | 再点火の可否・熱状態・姿勢制御能力を工程ごとに持つ |
| `FLIGHT 9 AND SHIP 36 REPORT` / 2025-08-15、`FLIGHT 9` 節 | F9 booster は迎角増大による燃料移送管への過大荷重が有力。Ship は加圧 diffuser の故障、ノーズ圧上昇、扉への荷重、姿勢異常に発展 | 扉、圧力、姿勢、放出、再点火の依存関係を一つの因果系列として試す |

一次資料: [SpaceX Updates](https://www.spacex.com/updates) の上記日付・見出し。
「報告された有力原因」と「完全に確定した原因」を混同しない。

## MissionOS に必要な完了条件

これは上記の実例から導く **設計案** であり、SpaceX の内部 flight rules を再現したものではない。

1. `payload_released`: 扉作動と各物体の離脱を、ID・時刻・位置速度・イベント根拠付きで記録する。
2. `insertion_verified`: ペイロード自身の軌道を検証する。Ship の高度だけで代用しない。
3. `contact_verified`: 通信経路、最終受信時刻、対象 ID を記録する。架空 ACK は fixture と表示する。
4. `commissioning_verified`: 姿勢・電源・熱・推進・軌道上昇の完了根拠を別管理する。
5. `service_verified`: 実際のサービス接続に対応する証拠を要求する。単なる laser link を顧客提供と呼ばない。
6. `return_verified`: 航法、姿勢、燃料、熱、接地点、接地速度を独立に検証する。
7. `recovery_verified` と `reuse_verified`: 着水、海上での浮遊、陸上回収、再飛行を分ける。

サブシステムは少なくとも booster、Ship、各衛星、地上／塔、通信系に分割する。
F6 は機体だけでなく塔の健全性でも分岐する例、F9 は放出と再点火を安全に省略する例である。
全体の成功値を一つに潰さず、payload 成果と booster／Ship 帰還を独立表示する。

## 校正前に不足している情報

- 実績としての時刻付き位置・速度・姿勢・推進剤量。公開予定表では埋めない。
- 公開映像の速度・高度表示が使う座標系、基準面、フィルター、表示遅延。
- 衛星の個別 ID、放出時刻、離脱速度、公開軌道要素と母機からの対応関係。
- 最新飛行の実測空力係数、TPS 材料定数、エンジン過渡特性、タンク状態、故障閾値。
- F14 配備衛星の全基 commissioning と顧客サービス開始を示す個別証拠。

公開情報で決まらない量は `unknown` または仮定範囲に置き、値を調整して成功した結果を
実機の妥当性確認とは呼ばない。映像の再現、運用シーケンスの再現、物理モデルの検証を
別々に受け入れる。
