# YAMLポリシーを試す

最初の例はfixture専用です。外部モデル、Gazebo、実機は動かしません。
リポジトリの開発環境をセットアップした後、リポジトリ直下で実行します。

```sh
export PYTHONPATH=packages/missionos-cli/src:.
.venv/bin/python -m missionos_cli assurance-policy validate examples/assurance-policy/fixture.yaml
```

表示された内容と`sha256`を確認します。検証だけでは承認になりません。
次の`<確認したsha256>`と`<操作者名>`を自分の値に置き換えて承認します。
DBにはミッション単位の実行回数も保存されるので、継続するときも同じDBを使います。

```sh
.venv/bin/python -m missionos_cli assurance-policy approve examples/assurance-policy/fixture.yaml \
  --db ./tmp/assurance-policy.db --operator '<操作者名>' --sha256 '<確認したsha256>'

.venv/bin/python -m missionos_cli assurance-policy fixture \
  --db ./tmp/assurance-policy.db --sha256 '<確認したsha256>' --proposal-id attempt-1
```

`authorization_source=human_approved_policy`、`human_approval_observed=false`、
`executor_invoked=true`が、このfixtureで期待する結果です。
`physical_execution_invoked=false`、`live_model_invoked=false`も確認してください。
`effect_observed`は合成実行対象に対する観測です。

同じ`proposal-id`でもう一度実行すると拒否します。異なるIDでも合計3回を超えると
拒否します。`--target-x 50`を付けた場合も、承認範囲外なので拒否します。
拒否時の終了コードは2です。

`mode`を`shadow`または`human`に変える場合は、新しいハッシュを確認して承認します。
これらのモードでは個別承認なしのfixture実行は拒否されます。
今回のfixture行動を個別に承認する場合は`--approve-action`を付けます。

今後のポリシー使用を取り消すには、次を実行します。

```sh
.venv/bin/python -m missionos_cli assurance-policy revoke \
  --db ./tmp/assurance-policy.db --sha256 '<確認したsha256>'
```

取消は既に送った行動を停止しません。取り消した同じ版の再承認も拒否します。

この例の有効期限は再現用に長く設定しています。実際のシミュレーターへの統合では、
そのミッションに適した短い有効期限と、観測に結び付く保護条件を設定してください。

## 通常のTurtleBot3シミュレータで使う

Gatewayの起動環境に`MISSIONOS_ASSURANCE_POLICY_DB`を設定し、
`MISSIONOS_TURTLEBOT3_RECOVERY_REQUIRES_APPROVAL=1`で復旧チェックポイントを
有効にします。`scripts/start_ros2_nav2_turtlebot3_gateway_docker.sh`はこれらを
コンテナへ渡します。DBはGatewayから見えるパスを指定します。

通常どおりミッションを計画し、最初のミッション承認を行います。
行動範囲の雛形は[このYAML](../../examples/assurance-policy/turtlebot3-scope.yaml)です。
`scope.yaml`へコピーし、地図座標の範囲・回数と短い有効期限を設定します。
雛形の期限は意図的に切れています。
計画の`scenario_proposal`と承認結果の`turtlebot3_home_mission_approval`を、
それぞれJSONファイルとして保存します。ポリシーの行動範囲を確認してから、
このミッション承認に結び付けます。

```sh
.venv/bin/python -m missionos_cli assurance-policy bind-turtlebot3 scope.yaml \
  --proposal-json proposal.json --approval-json initial-approval.json \
  --output bound-policy.yaml
```

生成されたYAMLとハッシュを確認し、`approve`コマンドで同じDBに承認を記録します。
その後、通常の`run`を一度実行します。承認した範囲内の復旧は、Gatewayの実行処理が
観測・判断・実行・検証を繰り返します。範囲外や上限到達なら、元のチェックポイントを
保持して人間へ戻します。

このアダプタで観測に結び付いている保護条件は`nav2_path_feasible`と
`mission_contract_unchanged`です。荷物の破損やバッテリー残量の保証としては使えません。
ポリシーには`execution_scope: simulator`を指定します。

比較検証用の実行スクリプトもあります。これはテスト実行の指示に基づいて
個別承認を送るため、通常の運用コマンドとは区別してください。

```sh
# 各モードの前に専用シミュレータを再起動する。
PYTHONPATH=.:packages/missionos-core/src:packages/missionos-cli/src \
python3 scripts/run_assurance_policy_turtlebot3_e2e.py \
  --mode human --url http://127.0.0.1:18791 \
  --output output/policy-human --db output/policies.db

PYTHONPATH=.:packages/missionos-core/src:packages/missionos-cli/src \
python3 scripts/run_assurance_policy_turtlebot3_e2e.py \
  --mode bounded --url http://127.0.0.1:18791 \
  --output output/policy-bounded --db output/policies.db
```

複数回の復旧を比較するときは、両方のGateway起動で
`MISSIONOS_TURTLEBOT3_SIMULATE_POST_RECOVERY_ROUTE_FAILURE_ONCE=1`と
`ROS2_NAV2_SIM_FAULT_CANCEL_AFTER_ACCEPT=1`を明示します。故障を要求した記録だけでなく、
実際のキャンセル結果と、その後の復旧結果を確認してください。
