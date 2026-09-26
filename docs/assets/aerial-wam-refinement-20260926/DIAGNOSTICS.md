# WAM候補画像の診断

飛行結果とは別に、予測を支える入力と採点法を調べた。モデルの選択を望ましい経路名へ寄せる重み調整はしていない。

## 実際の観測が支える範囲

記録済みRGB-Dとカメラ姿勢から候補姿勢への投影を再構成し、公開モデルと同じ4:3 crop・224×224 bilinear resizeを適用した。
8候補すべてで保存済み投影PNGと完全一致した（8bit最大誤差0）。
色が黒いことと未観測は区別し、有効な深度・投影位置からマスクを作った。
下表の平均観測重みは補間後のマスクの平均、完全支持率は補間値が1の画素の割合であり、通過可能面積ではない。

| 場面 | 候補 | 平均観測重み | 完全支持率 | 空の投影 |
|---|---|---:|---:|---|
| gap | forward | 26.75% | 3.33% | なし |
| gap | left_detour | 21.60% | 20.55% | なし |
| climb | climb | 0.67% | 0.51% | なし |
| climb | forward | 29.59% | 0.03% | なし |
| climb | left_detour | 28.43% | 24.19% | なし |
| detour | forward | 0.00% | 0.00% | あり |
| detour | left_detour | 0.00% | 0.00% | あり |
| detour | right_detour | 0.00% | 0.00% | あり |

5 mの上昇・側方候補では、現在のカメラ履歴から投影できない領域が多かった。
ANWMは未観測領域も生成できるため、未観測であるだけで予測が誤りとは断定しない。
ただし生成された空き領域を「センサーで確認した通路」とは扱えない。
有効画素が0なら支持領域の予測誤差を`null`とし、誤差0の完全一致には数えていない。

[測定値と画像・元入力のhash](summary.json)に全候補の値を保存した。
再構成は`python scripts/audit_urban_wam_candidates.py --input-dir "$RUN/input" --result "$RUN/forecast/result.json" --output "$AUDIT"`。
投影は記録RGB-D/姿勢だけを使用し、宣言された建物形状を使用していない。

## 予測距離を短くした場合の入力診断

初回能力試験の記録入力と supplied route を使い、1・2・3・5 m先への投影をCPUで再計算した。
上昇候補の平均観測重みは5 mで0.60%、2 mで61.48%。側方回避場面の3候補も5 mでは全て0%、2 mでは47.80〜80.00%だった。
これは**入力の視野が重なる割合の診断**であり、新しいANWM推論、実際の未来画像、飛行成功ではない。
短い移動と再観測は検討に値するが、この値だけでWAM選択が改善したとは言えない。
[全32候補姿勢の入力診断](view-support-probe.json)。

## 上流のLPIPS採点を試した結果

固定した[上流計画コード](https://github.com/EmbodiedCity/ANWM.code/blob/657a80268505fa9149c4df502e35aa0f5bce11e5/real/planning_eval.py)はLPIPS-AlexNetを使う。
本経路選択器のRGB MSEとは異なるため、先に[診断条件](goal-metric-audit-protocol.json)を固定し、保存済み生成画像をCPU上の実LPIPSで採点した。
同じcrop/resizeの目標画像と、8bit保存予測を使用した。重みのhash、モデル実行receipt、入力と予測のhashを各JSONに残した。

初回能力試験と今回の単一SSH試験の両方で、選択は同じ結果だった。

| 場面 | RGB MSE | LPIPS |
|---|---|---|
| gap | left_detour | left_detour |
| climb | left_detour | forward |
| detour | right_detour | right_detour |

LPIPSへの交換だけでは隙間通過・上昇の選択は改善しなかった。上昇場面のforwardも制約で拒否される候補である。
採点だけの診断なのでdispatchは変えておらず、新規ANWM推論・追加飛行は0。
[初回8画像対](lpips-aerial-wam-capability-20260926.json) · [新規8画像対](lpips-aerial-wam-refinement-20260926.json)。

待ち時間の改善と、動作を選べる能力は分けて評価する。距離・時刻をそろえた短い予測と実画像での確認が、選択改善に向けた次の候補となる。
現段階で事後学習が必須とは結論しない。Rulesへの優越性も要求しない。
