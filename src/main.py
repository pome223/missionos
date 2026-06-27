"""
boiled-claw メインエントリーポイント
CLI / Web 両対応
"""

import asyncio
import json
import os
from pathlib import Path
from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt
import click

from src.cli.missionos import missionos as missionos_command

load_dotenv()

console = Console()


def _require_api_key():
    """GOOGLE_API_KEY が設定されていなければエラー終了する。"""
    if not os.getenv("GOOGLE_API_KEY"):
        console.print(
            "[red]Error: GOOGLE_API_KEY is not set.[/red]\n"
            "Copy .env.example to .env and set your API key."
        )
        raise click.Abort()


class _AliasGroup(click.Group):
    """Support legacy command aliases (cli -> chat, host-bridge -> bridge host, etc.)."""

    # Simple aliases: old name -> new subcommand name
    _SIMPLE_ALIASES: dict[str, str] = {
        "cli": "chat",
    }
    # Multi-token aliases: old name -> replacement tokens
    _MULTI_ALIASES: dict[str, list[str]] = {
        "host-bridge": ["bridge", "host"],
        "desktop-bridge": ["bridge", "desktop"],
    }

    def resolve_command(self, ctx, args):
        """Rewrite legacy command names before resolution."""
        if args:
            cmd_name = args[0]
            if cmd_name in self._SIMPLE_ALIASES:
                args = [self._SIMPLE_ALIASES[cmd_name]] + args[1:]
            elif cmd_name in self._MULTI_ALIASES:
                args = self._MULTI_ALIASES[cmd_name] + args[1:]
        return super().resolve_command(ctx, args)


@click.group(cls=_AliasGroup, invoke_without_command=True)
@click.version_option(package_name="boiled-claw")
@click.option(
    "-v", "--verbose", is_flag=True, default=False, help="Enable verbose output."
)
@click.pass_context
def cli(ctx, verbose):
    """boiled-claw — Your personal AI agent powered by Gemini."""
    ctx.ensure_object(dict)
    ctx.obj["verbose"] = verbose
    if ctx.invoked_subcommand is None:
        ctx.invoke(chat)


cli.add_command(missionos_command)


# ── chat (default interactive REPL) ──────────────────────────────


@cli.command()
@click.option(
    "--model", default=None, help="Override the agent model (e.g. gemini-2.5-flash)."
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Validate config and exit without running.",
)
@click.pass_context
def chat(ctx, model, dry_run):
    """Start an interactive chat session (REPL)."""
    _require_api_key()
    verbose = ctx.obj.get("verbose", False)
    asyncio.run(_run_cli(model_override=model, verbose=verbose, dry_run=dry_run))


def _setup_readline():
    """readline 履歴を設定する。"""
    import readline
    from pathlib import Path

    history_file = Path.home() / ".boiled_claw_history"
    try:
        readline.read_history_file(history_file)
    except (FileNotFoundError, OSError):
        pass
    readline.set_history_length(1000)

    import atexit

    def _save_history():
        try:
            readline.write_history_file(str(history_file))
        except OSError:
            pass

    atexit.register(_save_history)


async def _run_cli(
    model_override: str | None = None,
    verbose: bool = False,
    dry_run: bool = False,
):
    """CLIモードでエージェントを実行する"""
    from google.adk.runners import Runner
    from google.genai import types
    from src.config.settings import get_settings
    from src.memory_lifecycle.adk_memory_service import get_promoted_memory_service
    from src.runtime.session_service import create_session_service
    from src.skills.runtime import ensure_skills_loaded
    from src.cli.repl import handle_slash_command

    _setup_readline()

    settings = get_settings()
    if model_override:
        settings.agent_model = model_override
        # Update the model config snapshot before root_agent is constructed
        import src.agents.model_config as _mc

        _mc._DEFAULT_MODEL_NAME = model_override
        _mc.DEFAULT_MODEL = _mc.GeminiModelConfig(name=model_override, temperature=0.7)
        _mc.PRECISE_MODEL = _mc.GeminiModelConfig(
            name=model_override, temperature=0.2, top_k=20
        )
        _mc.CREATIVE_MODEL = _mc.GeminiModelConfig(name=model_override, temperature=1.2)

    from src.agents.root_agent import root_agent

    await ensure_skills_loaded()

    if verbose:
        console.print(
            f"[dim]Config: model={settings.agent_model}, "
            f"host_bridge={settings.host_bridge_enabled}, "
            f"desktop_bridge={settings.desktop_bridge_enabled}[/dim]"
        )

    if dry_run:
        console.print("[green]Config OK. Dry-run mode — exiting.[/green]")
        return

    session_service = create_session_service(settings)
    memory_service = get_promoted_memory_service()
    runner = Runner(
        agent=root_agent,
        app_name="boiled-claw",
        session_service=session_service,
        memory_service=memory_service,
    )

    session = await session_service.create_session(
        app_name="boiled-claw",
        user_id="local_user",
    )

    console.print(
        Panel(
            "[bold cyan]boiled-claw[/bold cyan] 🦀\n"
            "Your personal AI agent powered by Gemini\n"
            f"[dim]Model: {settings.agent_model}[/dim]\n"
            "[dim]Type /help for commands, 'exit' to quit[/dim]",
            border_style="cyan",
        )
    )

    repl_ctx = dict(settings=settings, session=session, root_agent=root_agent)

    while True:
        try:
            user_input = Prompt.ask("\n[bold green]You[/bold green]")

            if user_input.lower() in ("exit", "quit", "q"):
                console.print("[dim]Goodbye! 👋[/dim]")
                break

            if not user_input.strip():
                continue

            # Slash commands
            if handle_slash_command(user_input, **repl_ctx):
                continue

            content = types.Content(role="user", parts=[types.Part(text=user_input)])

            console.print("\n[bold blue]boiled-claw[/bold blue] 🦀", end=" ")

            async for event in runner.run_async(
                user_id="local_user",
                session_id=session.id,
                new_message=content,
            ):
                if event.is_final_response():
                    if event.content and event.content.parts:
                        for part in event.content.parts:
                            if part.text:
                                console.print(part.text)

        except KeyboardInterrupt:
            console.print("\n[dim]Interrupted. Type 'exit' to quit.[/dim]")
        except Exception as e:
            console.print(f"[red]Error: {e}[/red]")


# ── web (Gateway server) ────────────────────────────────────────


@cli.command()
@click.option("--host", default=None, help="Bind host (default: 127.0.0.1)")
@click.option("--port", default=None, type=int, help="Bind port (default: 18789)")
@click.option("--model", default=None, help="Override the agent model.")
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Validate config and exit without running.",
)
@click.pass_context
def web(ctx, host, port, model, dry_run):
    """Start the WebSocket Gateway server."""
    from src.config.settings import get_settings
    from src.gateway.server import create_gateway

    settings = get_settings()
    if model:
        settings.agent_model = model

    verbose = ctx.obj.get("verbose", False)
    if verbose:
        console.print(
            f"[dim]Config: model={settings.agent_model}, "
            f"host={host or settings.gateway_host}, "
            f"port={port or settings.gateway_port}[/dim]"
        )

    if dry_run:
        console.print("[green]Config OK. Dry-run mode — exiting.[/green]")
        return

    if not os.getenv("GOOGLE_API_KEY"):
        console.print(
            "[yellow]![/yellow] GOOGLE_API_KEY is not set. Gateway can start, "
            "but model-backed chat will fail until a key is configured."
        )

    console.print(
        Panel(
            "[bold cyan]boiled-claw Gateway Server[/bold cyan] 🦀\n"
            f"WebSocket endpoint: ws://{host or settings.gateway_host}:{port or settings.gateway_port}/ws/{{user_id}}\n"
            "[dim]Press Ctrl+C to stop[/dim]",
            border_style="cyan",
        )
    )

    gateway = create_gateway()
    gateway.run(host=host, port=port)


# ── channels (Telegram / Discord) ───────────────────────────────


@cli.command()
def channels():
    """Start multi-channel mode (Telegram, Discord)."""
    _require_api_key()
    asyncio.run(_run_channels())


async def _run_channels():
    """チャネルモードで実行する"""
    from src.config.settings import get_settings
    from src.channels.registry import get_channel_registry
    from src.channels.telegram import TelegramChannel
    from src.channels.discord_ch import DiscordChannel
    from google.adk.runners import Runner
    from src.agents.root_agent import root_agent
    from google.genai import types
    from src.skills.runtime import ensure_skills_loaded
    from src.memory_lifecycle.adk_memory_service import get_promoted_memory_service
    from src.runtime.session_service import create_session_service

    settings = get_settings()
    await ensure_skills_loaded()
    registry = get_channel_registry()

    session_service = create_session_service(settings)
    memory_service = get_promoted_memory_service()
    runner = Runner(
        agent=root_agent,
        app_name="boiled-claw",
        session_service=session_service,
        memory_service=memory_service,
    )

    async def handle_message(msg):
        session = await session_service.create_session(
            app_name="boiled-claw",
            user_id=msg.user_id,
        )
        content = types.Content(role="user", parts=[types.Part(text=msg.content)])
        response_text = ""
        async for event in runner.run_async(
            user_id=msg.user_id,
            session_id=session.id,
            new_message=content,
        ):
            if event.is_final_response():
                if event.content and event.content.parts:
                    for part in event.content.parts:
                        if part.text:
                            response_text += part.text
        return response_text

    if settings.telegram_bot_token:
        try:
            telegram = TelegramChannel({"bot_token": settings.telegram_bot_token})
            telegram.set_message_handler(handle_message)
            registry.register_channel(telegram)
            console.print("[green]✓[/green] Telegram channel registered")
        except Exception as e:
            console.print(f"[yellow]![/yellow] Telegram channel failed: {e}")

    if settings.discord_bot_token:
        try:
            discord_ch = DiscordChannel({"bot_token": settings.discord_bot_token})
            discord_ch.set_message_handler(handle_message)
            registry.register_channel(discord_ch)
            console.print("[green]✓[/green] Discord channel registered")
        except Exception as e:
            console.print(f"[yellow]![/yellow] Discord channel failed: {e}")

    console.print(
        Panel(
            "[bold cyan]boiled-claw Channels[/bold cyan] 🦀\n"
            f"Active channels: {len(registry.list_channels())}\n"
            "[dim]Press Ctrl+C to stop[/dim]",
            border_style="cyan",
        )
    )

    await registry.start_all_channels()

    try:
        while True:
            await asyncio.sleep(1)
    except KeyboardInterrupt:
        console.print("\n[dim]Stopping channels...[/dim]")
        await registry.stop_all_channels()


# ── quickstart smoke ──────────────────────────────────────────────


@cli.command("quickstart-smoke")
@click.option(
    "--gateway-url", default="http://127.0.0.1:18789", help="Gateway base URL."
)
@click.option(
    "--user-id", default="quickstart", help="Owner user id for the smoke task."
)
@click.option(
    "--session-id",
    default="quickstart-local",
    help="Owner session id for the smoke task.",
)
@click.option(
    "--json",
    "json_output",
    is_flag=True,
    default=False,
    help="Print machine-readable JSON.",
)
def quickstart_smoke(gateway_url, user_id, session_id, json_output):
    """Create a deterministic no-model smoke task and timeline."""
    from src.quickstart_smoke import run_quickstart_smoke

    result = run_quickstart_smoke(
        gateway_url=gateway_url,
        user_id=user_id,
        session_id=session_id,
    )
    if json_output:
        click.echo(json.dumps(result, ensure_ascii=True, sort_keys=True))
        return

    console.print("[green]Quickstart smoke completed.[/green]")
    console.print(f"Task: {result['task_url']}")
    console.print(f"Timeline: {result['timeline_url']}")
    console.print(f"Control UI: {result['control_ui_url']}")
    console.print(
        "[dim]No GOOGLE_API_KEY, Chrome extension, Host Bridge, or Desktop Bridge required.[/dim]"
    )


# ── status ───────────────────────────────────────────────────────


@cli.command()
def status():
    """Show configuration, bridge connectivity, and registered tools."""
    import httpx
    from src.config.settings import get_settings
    from src.runtime.session_service import describe_session_backend

    settings = get_settings()
    session_backend = describe_session_backend(settings)

    # ── Configuration summary ──
    from rich.table import Table

    cfg_table = Table(title="Configuration", show_header=False)
    cfg_table.add_column("Key", style="cyan")
    cfg_table.add_column("Value")
    cfg_table.add_row("Agent Model", settings.agent_model)
    cfg_table.add_row("Gateway", f"{settings.gateway_host}:{settings.gateway_port}")
    cfg_table.add_row("Shell Enabled", str(settings.shell_enabled))
    cfg_table.add_row("Browser Headless", str(settings.browser_headless))
    cfg_table.add_row("Session Backend", session_backend["backend"])
    if session_backend["namespace"]:
        cfg_table.add_row("Redis Namespace", session_backend["namespace"])
    cfg_table.add_row("API Key Set", "yes" if os.getenv("GOOGLE_API_KEY") else "no")
    console.print(cfg_table)

    # ── Bridge connectivity ──
    bridge_table = Table(
        title="Bridge Status", show_header=True, header_style="bold cyan"
    )
    bridge_table.add_column("Bridge")
    bridge_table.add_column("Enabled")
    bridge_table.add_column("URL")
    bridge_table.add_column("Reachable")

    bridges = [
        ("Host Bridge", settings.host_bridge_enabled, settings.host_bridge_url),
        (
            "Desktop Bridge",
            settings.desktop_bridge_enabled,
            settings.desktop_bridge_url,
        ),
    ]

    for name, enabled, url in bridges:
        reachable = "-"
        if enabled and url:
            try:
                # Probe the actual SSE endpoint; a live FastMCP bridge returns 200
                # with content-type text/event-stream on GET /sse.
                resp = httpx.get(url, timeout=3, follow_redirects=True)
                is_sse = "text/event-stream" in resp.headers.get("content-type", "")
                if resp.status_code == 200 and is_sse:
                    reachable = "[green]yes[/green]"
                else:
                    reachable = f"[yellow]{resp.status_code}[/yellow]"
            except Exception:
                reachable = "[red]no[/red]"
        bridge_table.add_row(
            name,
            "[green]yes[/green]" if enabled else "[dim]no[/dim]",
            url or "-",
            reachable,
        )
    console.print(bridge_table)

    # ── Channel tokens ──
    ch_table = Table(title="Channels", show_header=True, header_style="bold cyan")
    ch_table.add_column("Channel")
    ch_table.add_column("Configured")
    ch_table.add_row(
        "Telegram",
        "[green]yes[/green]" if settings.telegram_bot_token else "[dim]no[/dim]",
    )
    ch_table.add_row(
        "Discord",
        "[green]yes[/green]" if settings.discord_bot_token else "[dim]no[/dim]",
    )
    ch_table.add_row(
        "Slack", "[green]yes[/green]" if settings.slack_bot_token else "[dim]no[/dim]"
    )
    console.print(ch_table)


@cli.group()
def eval():
    """Run trajectory-native eval specs and inspect reports."""
    pass


@eval.command("run")
@click.argument("spec_path", type=click.Path(exists=True, dir_okay=False))
@click.option(
    "--trajectory-id",
    type=int,
    default=None,
    help="Evaluate a single stored computer trajectory.",
)
@click.option(
    "--limit",
    type=int,
    default=None,
    help="Override the number of matching trajectories to include.",
)
def eval_run(spec_path, trajectory_id, limit):
    """Run a Phase 0 eval spec against stored computer trajectories."""
    from src.evals.runtime import run_eval_spec

    result = run_eval_spec(
        spec_path,
        trajectory_id=trajectory_id,
        limit=limit,
    )
    console.print_json(json.dumps(result, ensure_ascii=False, indent=2))
    if not result.get("success"):
        raise click.Abort()


@eval.command("report")
@click.option(
    "--task-id", default=None, help="Task id returned by `boiled-claw eval run`."
)
@click.option(
    "--eval-id",
    default=None,
    help="Eval id from the YAML spec. Returns the latest completed or failed matching report.",
)
@click.option(
    "--compare-to-task-id",
    default=None,
    help="Baseline eval task id to compare against.",
)
@click.option(
    "--compare-to-eval-id", default=None, help="Baseline eval id to compare against."
)
def eval_report(task_id, eval_id, compare_to_task_id, compare_to_eval_id):
    """Show a stored eval report by task id or eval id."""
    from src.evals.runtime import get_eval_report

    result = get_eval_report(
        task_id=task_id,
        eval_id=eval_id,
        compare_to_task_id=compare_to_task_id,
        compare_to_eval_id=compare_to_eval_id,
    )
    console.print_json(json.dumps(result, ensure_ascii=False, indent=2))
    if not result.get("success"):
        raise click.Abort()


@eval.command("classify")
@click.option(
    "--trajectory-id",
    type=int,
    required=True,
    help="Stored computer trajectory id to override.",
)
@click.option(
    "--failure-type",
    type=click.Choice(
        [
            "weak_evidence",
            "focus_mismatch",
            "target_context_mismatch",
            "unknown",
            "clear",
        ]
    ),
    required=True,
    help="Normalized failure type override, or `clear` to remove the operator override.",
)
def eval_classify(trajectory_id, failure_type):
    """Override the normalized failure classification for one stored trajectory."""
    from src.evals.runtime import override_trajectory_failure_type

    requested_failure_type = None if failure_type == "clear" else failure_type
    result = override_trajectory_failure_type(
        trajectory_id=trajectory_id,
        failure_type=requested_failure_type,
    )
    console.print_json(json.dumps(result, ensure_ascii=False, indent=2))
    if not result.get("success"):
        raise click.Abort()


@cli.command("self-improvement-demo")
@click.option(
    "--trajectory-id",
    type=int,
    required=True,
    help="Failed computer trajectory id to replay.",
)
@click.option(
    "--candidate-command",
    "candidate_commands",
    multiple=True,
    required=True,
    help="Command to apply the candidate change inside the canary. Repeat for multiple steps.",
)
@click.option(
    "--benchmark-command",
    "benchmark_commands",
    multiple=True,
    required=True,
    help="Benchmark command to gate the candidate. Repeat for multiple steps.",
)
@click.option(
    "--repo-path", default=None, help="Repository root to create the canary from."
)
@click.option("--base-ref", default="HEAD", help="Git ref used to seed the canary.")
@click.option(
    "--worktree-root",
    default=None,
    help="Directory where canary worktrees should be created.",
)
@click.option("--goal", default=None, help="Override the generated canary goal.")
@click.option(
    "--summary", default=None, help="Override the generated improvement summary."
)
@click.option(
    "--timeout-seconds",
    default=0,
    type=int,
    help="Timeout for candidate and benchmark commands.",
)
@click.option(
    "--promotion-kind",
    type=click.Choice(
        [
            "approved_improvement_memory",
            "approved_skill",
            "capability_patch",
            "policy_patch",
        ]
    ),
    default="approved_improvement_memory",
    help="Promotion artifact class to emit for a passing candidate.",
)
@click.option(
    "--approval-dependency",
    "approval_dependencies",
    multiple=True,
    help="Explicit approval id/ref required before recording approved_skill, capability_patch, or policy_patch.",
)
@click.option(
    "--record-as-approved",
    is_flag=True,
    default=False,
    help="Record a passing candidate as an approved promotion artifact or memory.",
)
@click.option(
    "--auto-cleanup",
    is_flag=True,
    default=False,
    help="Remove the canary after packaging.",
)
def self_improvement_demo(
    trajectory_id,
    candidate_commands,
    benchmark_commands,
    repo_path,
    base_ref,
    worktree_root,
    goal,
    summary,
    timeout_seconds,
    promotion_kind,
    approval_dependencies,
    record_as_approved,
    auto_cleanup,
):
    """Run one self-improvement demo flow from a failed computer trajectory."""
    from src.tools.self_improvement import self_improvement_demo_from_trajectory

    result = asyncio.run(
        self_improvement_demo_from_trajectory(
            trajectory_id=trajectory_id,
            candidate_commands="\n".join(candidate_commands),
            benchmark_commands="\n".join(benchmark_commands),
            repo_path=repo_path,
            base_ref=base_ref,
            worktree_root=worktree_root,
            goal=goal,
            improvement_summary=summary,
            timeout_seconds=timeout_seconds,
            record_as_approved=record_as_approved,
            promotion_kind=promotion_kind,
            approval_dependencies=list(approval_dependencies),
            auto_cleanup=auto_cleanup,
        )
    )
    console.print_json(json.dumps(result, ensure_ascii=False, indent=2))
    if not result.get("success"):
        raise click.Abort()


@cli.command("self-improvement-search")
@click.option(
    "--trajectory-id",
    type=int,
    required=True,
    help="Failed computer trajectory id to search from.",
)
@click.option(
    "--candidate-spec",
    "candidate_specs",
    multiple=True,
    required=True,
    help=(
        "JSON object describing one candidate, for example "
        '\'{"name":"small-fix","commands":["python3 scripts/apply_fix.py"]}\'. '
        "Repeat for multiple candidates."
    ),
)
@click.option(
    "--benchmark-command",
    "benchmark_commands",
    multiple=True,
    required=True,
    help="Benchmark command to gate every candidate. Repeat for multiple steps.",
)
@click.option(
    "--repo-path", default=None, help="Repository root to create the canary from."
)
@click.option("--base-ref", default="HEAD", help="Git ref used to seed the canary.")
@click.option(
    "--worktree-root",
    default=None,
    help="Directory where canary worktrees should be created.",
)
@click.option("--goal", default=None, help="Override the generated search goal.")
@click.option(
    "--summary", default=None, help="Override the generated improvement summary."
)
@click.option(
    "--timeout-seconds",
    default=0,
    type=int,
    help="Timeout for candidate and benchmark commands.",
)
@click.option(
    "--promotion-kind",
    type=click.Choice(
        [
            "approved_improvement_memory",
            "approved_skill",
            "capability_patch",
            "policy_patch",
        ]
    ),
    default="approved_improvement_memory",
    help="Promotion artifact class to emit for the winning candidate.",
)
@click.option(
    "--approval-dependency",
    "approval_dependencies",
    multiple=True,
    help="Explicit approval id/ref required before recording approved_skill, capability_patch, or policy_patch.",
)
@click.option(
    "--record-winner-as-approved",
    is_flag=True,
    default=False,
    help="Record the winning candidate as an approved promotion artifact or memory.",
)
@click.option(
    "--cleanup-losers/--keep-losers",
    default=True,
    help="Remove losing canaries after comparison.",
)
@click.option(
    "--auto-cleanup",
    is_flag=True,
    default=False,
    help="Also remove the winning canary after packaging.",
)
def self_improvement_search(
    trajectory_id,
    candidate_specs,
    benchmark_commands,
    repo_path,
    base_ref,
    worktree_root,
    goal,
    summary,
    timeout_seconds,
    promotion_kind,
    approval_dependencies,
    record_winner_as_approved,
    cleanup_losers,
    auto_cleanup,
):
    """Search across multiple canaries for the best repair candidate."""
    from src.tools.self_improvement import self_improvement_search_from_trajectory

    result = asyncio.run(
        self_improvement_search_from_trajectory(
            trajectory_id=trajectory_id,
            candidate_specs_json=json.dumps(
                [json.loads(spec) for spec in candidate_specs], ensure_ascii=False
            ),
            benchmark_commands="\n".join(benchmark_commands),
            repo_path=repo_path,
            base_ref=base_ref,
            worktree_root=worktree_root,
            goal=goal,
            improvement_summary=summary,
            timeout_seconds=timeout_seconds,
            record_winner_as_approved=record_winner_as_approved,
            promotion_kind=promotion_kind,
            approval_dependencies=list(approval_dependencies),
            cleanup_losers=cleanup_losers,
            auto_cleanup=auto_cleanup,
        )
    )
    console.print_json(json.dumps(result, ensure_ascii=False, indent=2))
    if not result.get("success"):
        raise click.Abort()


# ── mission review ───────────────────────────────────────────────


def _latest_mission_review_dir(root: str) -> Path:
    artifact_root = Path(root)
    candidates = (
        [
            item
            for item in artifact_root.iterdir()
            if item.is_dir() and (item / "report.redacted.json").exists()
        ]
        if artifact_root.exists()
        else []
    )
    if not candidates:
        raise click.ClickException(
            f"No mission review archives found under {artifact_root}."
        )
    return max(candidates, key=lambda path: path.stat().st_mtime)


def _load_json_file(path: str | None):
    if path is None:
        return None
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _load_review_archive(
    *,
    latest: bool,
    artifact_root: str,
    report_json: str | None,
    replay_index_json: str | None,
    safety_boundary_json: str | None,
    fleet_memory_provenance_json: str | None,
):
    explicit_paths = [
        report_json,
        replay_index_json,
        safety_boundary_json,
        fleet_memory_provenance_json,
    ]
    if latest and any(path is not None for path in explicit_paths):
        raise click.ClickException(
            "Use either --latest or explicit review JSON paths, not both."
        )
    archive_dir = None
    if latest:
        archive_dir = _latest_mission_review_dir(artifact_root)
        report_json = str(archive_dir / "report.redacted.json")
        replay_path = archive_dir / "replay_index.redacted.json"
        safety_path = archive_dir / "safety_boundary.redacted.json"
        provenance_path = archive_dir / "fleet_memory_provenance.redacted.json"
        replay_index_json = str(replay_path) if replay_path.exists() else None
        safety_boundary_json = str(safety_path) if safety_path.exists() else None
        fleet_memory_provenance_json = (
            str(provenance_path) if provenance_path.exists() else None
        )
    if report_json is None:
        raise click.ClickException("Use --latest or provide --report-json.")
    return {
        "archive_dir": str(archive_dir) if archive_dir else None,
        "report": _load_json_file(report_json),
        "replay_index": _load_json_file(replay_index_json),
        "safety_boundary": _load_json_file(safety_boundary_json),
        "fleet_memory_provenance": _load_json_file(fleet_memory_provenance_json),
    }


@cli.group()
def mission():
    """Mission OS review and reporting utilities."""
    pass


@mission.command("review")
@click.option(
    "--latest",
    is_flag=True,
    default=False,
    help="Read the newest local redacted mission review archive.",
)
@click.option(
    "--artifact-root",
    default="output/mission_control_review_reports",
    help="Directory containing local mission review archives.",
)
@click.option(
    "--report-json",
    default=None,
    type=click.Path(exists=True, dir_okay=False),
    help="Explicit redacted report JSON to render.",
)
@click.option(
    "--replay-index-json",
    default=None,
    type=click.Path(exists=True, dir_okay=False),
    help="Optional redacted replay index JSON for HTML timeline rendering.",
)
@click.option(
    "--safety-boundary-json",
    default=None,
    type=click.Path(exists=True, dir_okay=False),
    help="Optional redacted safety boundary JSON for HTML summary rendering.",
)
@click.option(
    "--fleet-memory-provenance-json",
    default=None,
    type=click.Path(exists=True, dir_okay=False),
    help="Optional redacted fleet memory provenance JSON.",
)
@click.option(
    "--format",
    "output_format",
    type=click.Choice(["json", "markdown", "html"]),
    default="markdown",
    show_default=True,
    help="Report output format.",
)
@click.option(
    "--output",
    default=None,
    type=click.Path(dir_okay=False),
    help="Write rendered report to a file instead of stdout.",
)
def mission_review(
    latest,
    artifact_root,
    report_json,
    replay_index_json,
    safety_boundary_json,
    fleet_memory_provenance_json,
    output_format,
    output,
):
    """Render a redacted Mission OS evidence review report."""
    from src.runtime.px4_gazebo_mission_review import (
        PX4GazeboMissionRunEvidenceReport,
        render_px4_gazebo_mission_report_html,
        render_px4_gazebo_mission_report_markdown,
        validate_px4_gazebo_mission_review_archive_consistency,
    )

    archive = _load_review_archive(
        latest=latest,
        artifact_root=artifact_root,
        report_json=report_json,
        replay_index_json=replay_index_json,
        safety_boundary_json=safety_boundary_json,
        fleet_memory_provenance_json=fleet_memory_provenance_json,
    )
    report = PX4GazeboMissionRunEvidenceReport.model_validate(archive["report"])
    validate_px4_gazebo_mission_review_archive_consistency(
        report=report,
        replay_index=archive["replay_index"],
        safety_boundary_summary=archive["safety_boundary"],
        fleet_memory_provenance=archive["fleet_memory_provenance"],
    )
    if output_format == "json":
        rendered = json.dumps(report.model_dump(mode="json"), indent=2, sort_keys=True)
    elif output_format == "html":
        rendered = render_px4_gazebo_mission_report_html(
            report,
            replay_index=archive["replay_index"],
            safety_boundary_summary=archive["safety_boundary"],
            fleet_memory_provenance=archive["fleet_memory_provenance"],
        )
    else:
        rendered = render_px4_gazebo_mission_report_markdown(report)

    if output:
        path = Path(output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rendered, encoding="utf-8")
        click.echo(str(path))
    else:
        click.echo(rendered)


# ── bridge group (host / desktop) ───────────────────────────────


@cli.group()
def bridge():
    """Manage bridge services (host, desktop)."""
    pass


@bridge.command("host")
@click.option("--host", default=None, help="Bind host (default: 127.0.0.1)")
@click.option("--port", default=None, type=int, help="Bind port (default: 8766)")
@click.option(
    "--transport",
    type=click.Choice(["sse", "stdio"]),
    default="sse",
    help="Transport mode (default: sse)",
)
def bridge_host(host, port, transport):
    """Start the Host Bridge MCP server."""
    from src.mcp_servers.host_bridge_server import create_server

    bind_host = host or "127.0.0.1"
    bind_port = port or 8766

    if transport == "stdio":
        console.print(
            Panel(
                "[bold cyan]boiled-claw Host Bridge[/bold cyan] 🦀\n"
                "Transport: stdio\n"
                "[dim]Use from a local MCP stdio client[/dim]",
                border_style="cyan",
            )
        )
        create_server(host="stdio").run(transport="stdio")
        return

    console.print(
        Panel(
            "[bold cyan]boiled-claw Host Bridge[/bold cyan] 🦀\n"
            f"SSE endpoint: http://{bind_host}:{bind_port}/sse\n"
            "[dim]Run this on the host OS, outside Docker[/dim]",
            border_style="cyan",
        )
    )
    create_server(host=bind_host, port=bind_port).run(transport="sse")


@bridge.command("desktop")
@click.option("--host", default=None, help="Bind host (default: 127.0.0.1)")
@click.option("--port", default=None, type=int, help="Bind port (default: 8767)")
@click.option(
    "--transport",
    type=click.Choice(["sse", "stdio"]),
    default="sse",
    help="Transport mode (default: sse)",
)
def bridge_desktop(host, port, transport):
    """Start the Desktop Bridge MCP server."""
    from src.mcp_servers.desktop_bridge_server import create_server

    bind_host = host or "127.0.0.1"
    bind_port = port or 8767

    if transport == "stdio":
        console.print(
            Panel(
                "[bold cyan]boiled-claw Desktop Bridge[/bold cyan] 🦀\n"
                "Transport: stdio\n"
                "[dim]Desktop client adapter. Control capabilities are still incomplete.[/dim]",
                border_style="cyan",
            )
        )
        create_server(host="stdio").run(transport="stdio")
        return

    console.print(
        Panel(
            "[bold cyan]boiled-claw Desktop Bridge[/bold cyan] 🦀\n"
            f"SSE endpoint: http://{bind_host}:{bind_port}/sse\n"
            "[dim]Run on the host OS. View-only desktop capabilities can be enabled first.[/dim]",
            border_style="cyan",
        )
    )
    create_server(host=bind_host, port=bind_port).run(transport="sse")


# ── entry point ──────────────────────────────────────────────────


def main():
    """メイン関数"""
    cli()


if __name__ == "__main__":
    main()
