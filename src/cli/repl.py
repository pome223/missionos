"""
REPL slash-command handler for the interactive chat session.
"""

from __future__ import annotations

from rich.console import Console
from rich.table import Table

console = Console()


def _cmd_help(**_ctx) -> bool:
    """Show available slash commands."""
    table = Table(title="REPL Commands", show_header=True, header_style="bold cyan")
    table.add_column("Command", style="green")
    table.add_column("Description")
    for name, (fn, _) in sorted(SLASH_COMMANDS.items()):
        table.add_row(f"/{name}", fn.__doc__ or "")
    console.print(table)
    return True


def _cmd_status(**ctx) -> bool:
    """Show current session status."""
    settings = ctx.get("settings")
    session = ctx.get("session")
    table = Table(title="Session Status", show_header=False)
    table.add_column("Key", style="cyan")
    table.add_column("Value")
    table.add_row("Model", settings.agent_model if settings else "unknown")
    table.add_row("Session ID", session.id if session else "unknown")
    table.add_row("User ID", "local_user")
    table.add_row("Host Bridge", "enabled" if settings and settings.host_bridge_enabled else "disabled")
    table.add_row("Desktop Bridge", "enabled" if settings and settings.desktop_bridge_enabled else "disabled")
    console.print(table)
    return True


def _cmd_tools(**ctx) -> bool:
    """List registered agent tools."""
    root_agent = ctx.get("root_agent")
    if root_agent is None:
        console.print("[yellow]Agent not available.[/yellow]")
        return True
    tools = getattr(root_agent, "tools", [])
    if not tools:
        console.print("[dim]No tools registered.[/dim]")
        return True
    table = Table(title="Registered Tools", show_header=True, header_style="bold cyan")
    table.add_column("#", style="dim")
    table.add_column("Tool")
    for i, tool in enumerate(tools, 1):
        name = getattr(tool, "name", None) or getattr(tool, "__name__", str(tool))
        table.add_row(str(i), name)
    console.print(table)
    return True


def _cmd_clear(**_ctx) -> bool:
    """Clear the terminal screen."""
    console.clear()
    return True


# Registry: name -> (handler, aliases)
SLASH_COMMANDS: dict[str, tuple] = {
    "help": (_cmd_help, ["h", "?"]),
    "status": (_cmd_status, ["s"]),
    "tools": (_cmd_tools, ["t"]),
    "clear": (_cmd_clear, ["cls"]),
}

# Build alias lookup
_ALIAS_MAP: dict[str, str] = {}
for _name, (_fn, _aliases) in SLASH_COMMANDS.items():
    for _alias in _aliases:
        _ALIAS_MAP[_alias] = _name


def handle_slash_command(raw_input: str, **ctx) -> bool | None:
    """Handle a slash command. Returns True if handled, None if not a slash command."""
    if not raw_input.startswith("/"):
        return None
    tokens = raw_input[1:].strip().split()
    if not tokens:
        console.print("[yellow]Type /help for available commands.[/yellow]")
        return True
    cmd_name = tokens[0].lower()
    # Resolve alias
    cmd_name = _ALIAS_MAP.get(cmd_name, cmd_name)
    entry = SLASH_COMMANDS.get(cmd_name)
    if entry is None:
        console.print(f"[yellow]Unknown command: /{cmd_name}. Type /help for available commands.[/yellow]")
        return True
    handler, _ = entry
    return handler(**ctx)
