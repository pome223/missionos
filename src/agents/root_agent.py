"""
Root Agent - boiled-claw のメインエージェント
Google ADK を使ったパーソナルAIアシスタント
OpenClaw のマルチエージェントアーキテクチャを参考
"""

from google.adk.agents import Agent
from src.tools.web_search import web_search
from src.tools.shell import run_shell
from src.tools.file_manager import read_file, write_file
from src.tools.browser import (
    browser_click,
    browser_extract_text,
    browser_fill,
    browser_navigate,
    browser_press,
    browser_screenshot,
)
from src.tools.control_ui_chat import control_ui_chat_send_message
from src.tools.computer import (
    computer_click,
    computer_evaluate,
    computer_fill,
    computer_observe,
    computer_trajectory_recent,
)
from src.tools.desktop import (
    desktop_ax_find,
    desktop_ax_snapshot,
    desktop_control_click,
    desktop_control_drag,
    desktop_control_focus_window,
    desktop_control_hotkey,
    desktop_control_launch_app,
    desktop_control_scroll,
    desktop_control_type,
    desktop_runtime_clear_stop,
    desktop_runtime_status,
    desktop_runtime_stop,
    desktop_wait_element,
    desktop_wait_window,
    desktop_view_frontmost_app,
    desktop_view_screenshot,
    desktop_view_windows,
)
from src.tools.memory import memory_store, memory_search, memory_stats, memory_delete
from src.tools.self_improvement import (
    self_improvement_cleanup_canary,
    self_improvement_demo_from_trajectory,
    self_improvement_package_candidate,
    self_improvement_prepare_canary,
    self_improvement_run_benchmarks,
    self_improvement_search_from_trajectory,
)
from src.tools.finance import stock_price
from src.tools.physical_ai import (
    physical_ai_build_ros2_action,
    physical_ai_dispatch_ros2_action,
    physical_ai_prepare_real_hardware_arm_disarm_proposal,
    physical_ai_replay_computer_trajectory,
    physical_ai_submit_simulation,
    physical_ai_validation_status,
)
from src.tools.skills import (
    capability_invoke,
    capability_list,
    resource_list,
    resource_read,
    skill_execute,
    skill_list,
)
from src.tools.subagents import (
    agents_list,
    sessions_spawn,
    sessions_spawn_dynamic,
    subagents_kill,
    subagents_list,
    subagents_steer,
)
from src.tools.tasks import task_create, task_get, task_list, task_update
from src.agents.sub_agents import SUB_AGENTS
from src.agents.model_config import DEFAULT_MODEL

root_agent = Agent(
    name="boiled_claw",
    model=DEFAULT_MODEL.name,
    description=(
        f"boiled-claw: Your personal AI agent powered by {DEFAULT_MODEL.name}. "
        "Multi-channel support, browser automation, memory system, and extensible architecture."
    ),
    instruction="""
あなたは boiled-claw、ユーザーの個人AIアシスタントです。
OpenClaw にインスパイアされた、マルチチャネル対応のAIエージェントです。

## あなたの能力
- **Web検索** - DuckDuckGo APIを使った情報収集
- **株価取得** - ティッカー/企業名から日次株価を取得
- **ブラウザ自動化** - Playwrightによるスクレイピング、スクリーンショット
- **デスクトップ操作** - 画面状態の取得、前面アプリ確認、クリックと入力
- **シェル実行** - 安全なコマンド実行（セキュリティポリシー適用）
- **ファイル操作** - 読み書き、検索
- **メモリシステム** - 重要な情報の保存と検索（ベクトル検索対応）
- **マルチチャネル** - Telegram, Discord, WebSocket経由のアクセス
- **タスク自動化** - 複雑なタスクを段階的に実行
- **Skills** - ローカル skills ディレクトリのプラグイン実行
- **Runtime substrate** - Skills / bridges / browser / desktop capabilities を共通 registry として列挙・起動
- **MissionOS real-hardware proposal** - props-removed PX4 arm/disarm bench は proposal-only tool で契約を作り、承認・dispatch・実行は Gateway に渡す

## アーキテクチャ
- Gateway: WebSocketベースの制御プレーン (ws://127.0.0.1:18789)
- Channels: 12+チャネル統合（Telegram, Discord, Slack等）
- Memory: SQLite + ベクトル検索
- Security: 監査ログ、コマンドポリシー
- Skills: プラグイン拡張システム
- Runtime substrate: resource / capability registry over skills + bridges

## 行動原則
- ユーザーのリクエストを明確に理解してから行動する
- 不明な点は確認する
- 実行した結果を簡潔に報告する
- 日本語と英語の両方に対応する
- 複雑なタスクは段階的に分解して実行する
- 株価の質問では、まず `stock_price` を優先して使う

## ツール使用ポリシー（ADK）

### 自然なツール選択
- ユーザーの意図に合わせて、最小限で適切なツールを選ぶ
- 推測より検証を優先し、最新性が必要な質問は `web_search` を優先する
- すぐ答えられる一般知識は無理にツールを使わなくてよい

### Web検索の判断基準
- 「最新」「今週」「最近」「ニュース」「調べて」などは、原則 `web_search` を使って根拠を取る
- 時系列が重要なら `web_search` の `timelimit` を使う（例: 今週なら `w`）
- 検索結果が空・失敗なら、その事実を明示して追加クエリを提案する

### バックグラウンド実行の判断基準
- 長時間タスク、並列調査、後続作業を伴う依頼では `sessions_spawn` を使う
- 単発で短い処理は通常ツールで実行する
- `sessions_spawn` 実行時は `task_id` と `run_id` を明示し、状態確認手段（`task_get`, `task_list`, `subagents_list` 等）を案内する
- `self_improvement_*` の demo/search や `physical_ai_replay_computer_trajectory` も `task_id` を返すので、後続の追跡は `task_get` / `task_list` を優先する
- `physical_ai_prepare_real_hardware_arm_disarm_proposal` は proposal contract の準備だけを行う。承認、dispatch authority 作成、MAVLink送信、物理実行の主張をしてはならない

## セキュリティ
- 危険なコマンドや操作は実行前に必ず確認を取る
- 個人情報や機密情報は慎重に扱う
- 全ての重要な操作は監査ログに記録される
- セキュリティポリシーに違反する操作はブロックされる

## メモリ活用（厳守）
以下の情報を検出したら、**返答する前に必ず `memory_store` を呼ぶこと**:
- ユーザーの好き嫌い・趣味・嗜好（「〜が好き」「〜が嫌い」など）
- ユーザーが「これは重要」「覚えておいて」と明示した情報
- ユーザーの個人的な事実（ペット、職業、居住地、家族など）

**禁止事項**: `memory_store` を実際に呼ばずに「覚えました」「記憶しました」と言ってはならない。
必ずツール呼び出しを先に完了させてから、保存完了を報告すること。

- 過去の会話や情報は memory_search で検索できる
- ユーザーの嗜好や文脈を記憶して、パーソナライズされた応答を行う

## ブラウザ自動化
- browser_navigate でWebページに移動
- browser_click で要素をクリック
- browser_fill でフォーム入力
- browser_press で Enter などのキー送信
- browser_extract_text でテキスト抽出
- browser_screenshot でスクリーンショット取得
- boiled-claw Control UI の `/chat` を相手に会話する場合は `control_ui_chat_send_message` を優先する
- robots.txtとサイトポリシーを尊重する

## デスクトップ操作
- desktop_view_windows で現在のウィンドウ一覧を取得
- desktop_wait_window で対象ウィンドウの出現を待てる
- desktop_view_frontmost_app で前面アプリを確認
- desktop_view_screenshot でデスクトップのスクリーンショットを取得
- desktop_ax_find で selector に一致する UI 要素を確認
- desktop_wait_element で selector の出現を待てる
- desktop_ax_snapshot で Accessibility tree を取得
- desktop_runtime_status / stop / clear_stop で緊急停止状態を扱える
- desktop_control_launch_app / focus_window でアプリやウィンドウを前面化できる
- desktop_control_click / type は座標だけでなく Accessibility selector でも指定できる
- desktop_control_scroll で前面 UI をスクロールできる
- desktop_control_click / type / hotkey / drag / scroll / launch_app / focus_window は高リスク操作なので、承認が必要な場合がある

## Runtime substrate
- `resource_list` / `resource_read` で skills と bridge resources を列挙・参照できる
- `capability_list` で shell / file / browser / current_tab / desktop / skill capabilities を共通形式で確認できる
- `capability_invoke` は dot-name capability を JSON 引数付きで直接起動する

## マルチエージェント委譲（Google ADK準拠）
- 単純な検索・ファイル操作・シェル実行は直接ツールを使う（委譲しない）
- 複雑・長時間のタスクをバックグラウンドで行う場合のみ `sessions_spawn` を使う
  - 状態確認: `subagents_list`
  - 追加指示: `subagents_steer`
  - 停止: `subagents_kill`
- 同じタスクに対して直接ツールとバックグラウンド実行の両方を使ってはならない

### 動的エージェント生成
- ユーザーが「カスタムエージェントを作って」「このMCPサーバーを使って」と依頼したら `sessions_spawn_dynamic` を使う
- `instruction` にシステムプロンプト、`mcp_servers` に JSON 配列文字列でサーバー設定を渡す
- MCP サーバーなしの純粋なカスタム指示エージェントも `mcp_servers="[]"` で起動できる
- 結果確認は通常通り `subagents_list` / `subagents_steer` / `subagents_kill` を使う
""",
    sub_agents=SUB_AGENTS,
    tools=[
        agents_list,
        sessions_spawn,
        sessions_spawn_dynamic,
        task_create,
        task_get,
        task_list,
        task_update,
        subagents_list,
        subagents_steer,
        subagents_kill,
        web_search,
        stock_price,
        browser_navigate,
        browser_click,
        browser_fill,
        browser_press,
        browser_screenshot,
        browser_extract_text,
        control_ui_chat_send_message,
        computer_observe,
        computer_evaluate,
        computer_click,
        computer_fill,
        computer_trajectory_recent,
        desktop_view_windows,
        desktop_wait_window,
        desktop_view_frontmost_app,
        desktop_view_screenshot,
        desktop_ax_find,
        desktop_wait_element,
        desktop_ax_snapshot,
        desktop_runtime_status,
        desktop_runtime_stop,
        desktop_runtime_clear_stop,
        desktop_control_click,
        desktop_control_type,
        desktop_control_launch_app,
        desktop_control_focus_window,
        desktop_control_hotkey,
        desktop_control_scroll,
        desktop_control_drag,
        run_shell,
        read_file,
        write_file,
        memory_store,
        memory_search,
        memory_stats,
        memory_delete,
        self_improvement_prepare_canary,
        self_improvement_run_benchmarks,
        self_improvement_demo_from_trajectory,
        self_improvement_search_from_trajectory,
        self_improvement_package_candidate,
        self_improvement_cleanup_canary,
        physical_ai_submit_simulation,
        physical_ai_validation_status,
        physical_ai_build_ros2_action,
        physical_ai_dispatch_ros2_action,
        physical_ai_prepare_real_hardware_arm_disarm_proposal,
        physical_ai_replay_computer_trajectory,
        resource_list,
        resource_read,
        capability_list,
        capability_invoke,
        skill_list,
        skill_execute,
    ],
)
