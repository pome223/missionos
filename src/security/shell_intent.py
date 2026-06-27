"""Shell command parsing and high-level intent classification."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Literal, Optional
import shlex


ShellRisk = Literal["low", "medium", "high"]

_CONTROL_OPERATORS = {"|", "||", "&", "&&", ";", ";;"}
_REDIRECTION_OPERATORS = {"<", ">", ">>", "<<", ">|"}
_SHELL_WRAPPER_FLAGS = {"-c", "-lc", "-ic", "-Command", "-EncodedCommand", "/c"}
_SHELL_WRAPPERS = {
    "bash",
    "sh",
    "zsh",
    "fish",
    "pwsh",
    "powershell",
    "cmd",
}
_INLINE_EVAL_FLAGS = {
    "python": {"-c"},
    "python3": {"-c"},
    "node": {"-e", "-p"},
    "ruby": {"-e"},
    "perl": {"-e"},
    "php": {"-r"},
}
_SEARCH_COMMANDS = {"rg", "grep", "find", "fd", "ack", "ag"}
_READ_COMMANDS = {"cat", "head", "tail", "sed", "awk", "cut", "sort", "uniq", "wc", "tr"}
_INSPECT_COMMANDS = {"ls", "pwd", "which", "whereis", "type", "date", "uname", "whoami", "id", "env", "printenv", "stat", "du", "df", "ps"}
_FILE_WRITE_COMMANDS = {"touch", "mkdir", "cp", "mv", "install", "tee", "ln", "chmod", "chown"}
_NETWORK_COMMANDS = {"curl", "wget", "nc", "ssh", "scp", "sftp", "ftp", "rsync"}
_PROCESS_COMMANDS = {"kill", "pkill", "killall", "nohup", "systemctl", "service", "launchctl"}
_PACKAGE_COMMANDS = {"apt", "apt-get", "brew", "pip", "pip3", "npm", "pnpm", "yarn", "bun", "cargo", "go", "uv"}
_TEST_SUBCOMMANDS = {"test", "pytest", "tox", "nox"}
_BUILD_SUBCOMMANDS = {"build", "compile", "check"}
_VCS_READ_SUBCOMMANDS = {
    "status",
    "log",
    "diff",
    "show",
    "rev-parse",
    "grep",
    "branch",
    "remote",
}
_PACKAGE_QUERY_SUBCOMMANDS = {"list", "info", "show", "view", "why", "outdated"}


def normalize_shell_command(command: str) -> str:
    """Keep quoted spacing intact while trimming surrounding whitespace."""

    return command.strip()


@dataclass
class ShellRedirection:
    operator: str
    target: str
    stream: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass
class ShellAst:
    tokens: list[str]
    exec_tokens: list[str]
    executable: Optional[str]
    executable_basename: Optional[str]
    control_operators: list[str] = field(default_factory=list)
    redirections: list[ShellRedirection] = field(default_factory=list)
    shell_wrapper_flag: Optional[str] = None
    inline_eval_flag: Optional[str] = None

    @property
    def has_shell_features(self) -> bool:
        return bool(self.control_operators or self.redirections)

    @property
    def uses_shell_wrapper(self) -> bool:
        return self.executable_basename in _SHELL_WRAPPERS and self.shell_wrapper_flag is not None

    @property
    def uses_inline_eval(self) -> bool:
        return self.inline_eval_flag is not None

    def to_dict(self) -> dict[str, object]:
        return {
            "tokens": list(self.tokens),
            "exec_tokens": list(self.exec_tokens),
            "executable": self.executable,
            "executable_basename": self.executable_basename,
            "control_operators": list(self.control_operators),
            "redirections": [asdict(item) for item in self.redirections],
            "shell_wrapper_flag": self.shell_wrapper_flag,
            "inline_eval_flag": self.inline_eval_flag,
            "has_shell_features": self.has_shell_features,
        }


@dataclass
class ShellIntent:
    category: str
    risk: ShellRisk
    summary: str
    read_only: bool

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass
class ShellCommandInspection:
    normalized: str
    ast: ShellAst
    intent: ShellIntent

    def validation_error(self) -> Optional[str]:
        if self.ast.control_operators:
            joined = ", ".join(self.ast.control_operators)
            return f"Shell control operators are not supported: {joined}"
        if self.ast.redirections:
            joined = ", ".join(redir.operator for redir in self.ast.redirections)
            return f"Shell redirection is not supported: {joined}"
        if self.ast.uses_shell_wrapper:
            executable = self.ast.executable_basename or self.ast.executable or "shell"
            return f"Shell wrapper with inline command is blocked: {executable} {self.ast.shell_wrapper_flag}"
        if self.ast.uses_inline_eval:
            executable = self.ast.executable_basename or self.ast.executable or "interpreter"
            return f"Inline interpreter evaluation is blocked: {executable} {self.ast.inline_eval_flag}"
        return None

    def to_dict(self) -> dict[str, object]:
        return {
            "normalized": self.normalized,
            "ast": self.ast.to_dict(),
            "intent": self.intent.to_dict(),
        }


def _tokenize_shell_command(command: str) -> list[str]:
    lexer = shlex.shlex(command, posix=True, punctuation_chars="|&;<>")
    lexer.whitespace_split = True
    return list(lexer)


def _parse_shell_ast(command: str) -> ShellAst:
    tokens = _tokenize_shell_command(command)
    exec_tokens: list[str] = []
    control_operators: list[str] = []
    redirections: list[ShellRedirection] = []

    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token in _CONTROL_OPERATORS:
            control_operators.append(token)
            index += 1
            continue

        stream = "stdout"
        operator = token
        if token.isdigit() and index + 1 < len(tokens) and tokens[index + 1] in _REDIRECTION_OPERATORS:
            operator = f"{token}{tokens[index + 1]}"
            stream = {"0": "stdin", "1": "stdout", "2": "stderr"}.get(token, f"fd:{token}")
            index += 1
            token = tokens[index]
        if token in _REDIRECTION_OPERATORS or operator != token:
            if operator == token:
                stream = "stdin" if token in {"<", "<<"} else "stdout"
            target = tokens[index + 1] if index + 1 < len(tokens) else ""
            redirections.append(ShellRedirection(operator=operator, target=target, stream=stream))
            index += 2
            continue

        exec_tokens.append(token)
        index += 1

    executable = exec_tokens[0] if exec_tokens else None
    executable_basename = executable.lstrip("./").split("/")[-1] if executable else None
    shell_wrapper_flag = _find_leading_flag_match(exec_tokens[1:], _SHELL_WRAPPER_FLAGS)
    inline_eval_flag = None
    if executable_basename in _INLINE_EVAL_FLAGS:
        inline_eval_flag = _find_leading_flag_match(
            exec_tokens[1:],
            _INLINE_EVAL_FLAGS[executable_basename],
        )

    return ShellAst(
        tokens=tokens,
        exec_tokens=exec_tokens,
        executable=executable,
        executable_basename=executable_basename,
        control_operators=control_operators,
        redirections=redirections,
        shell_wrapper_flag=shell_wrapper_flag,
        inline_eval_flag=inline_eval_flag,
    )


def _classify_git_intent(ast: ShellAst) -> ShellIntent:
    subcommand = ast.exec_tokens[1] if len(ast.exec_tokens) > 1 else ""
    if subcommand in _VCS_READ_SUBCOMMANDS:
        return ShellIntent("vcs_read", "low", f"Read-only git command: {subcommand}", True)
    return ShellIntent("vcs_write", "high", f"Repository-modifying git command: {subcommand or 'unknown'}", False)


def _find_leading_flag_match(args: list[str], allowed_flags: set[str]) -> Optional[str]:
    for arg in args:
        if arg == "--":
            break
        for flag in allowed_flags:
            if arg == flag or (arg.startswith(flag) and len(arg) > len(flag)):
                return flag
        if not arg.startswith("-") and not arg.startswith("/"):
            break
    return None


def _classify_package_intent(ast: ShellAst) -> ShellIntent:
    executable = ast.executable_basename or "package-manager"
    subcommands = [token.lower() for token in ast.exec_tokens[1:]]
    subcommand = subcommands[0] if subcommands else ""
    nested = subcommands[1] if len(subcommands) > 1 else ""

    if subcommand in _PACKAGE_QUERY_SUBCOMMANDS:
        return ShellIntent("package_query", "medium", f"Package manager query via {executable}", True)
    if subcommand in _TEST_SUBCOMMANDS or (subcommand == "run" and nested in _TEST_SUBCOMMANDS):
        return ShellIntent("test", "low", f"Test workflow via {executable}", True)
    if subcommand in _BUILD_SUBCOMMANDS or (subcommand == "run" and nested in _BUILD_SUBCOMMANDS):
        return ShellIntent("build", "medium", f"Build workflow via {executable}", False)
    return ShellIntent("package_management", "high", f"Package manager mutation via {executable}", False)


def classify_shell_command(ast: ShellAst) -> ShellIntent:
    executable = ast.executable_basename or ""
    lower_tokens = [token.lower() for token in ast.exec_tokens[1:]]

    if ast.uses_shell_wrapper:
        return ShellIntent("shell_wrapper", "high", f"Inline shell wrapper via {executable}", False)
    if ast.uses_inline_eval:
        return ShellIntent("interpreter_eval", "high", f"Inline interpreter evaluation via {executable}", False)
    if executable in _SEARCH_COMMANDS:
        return ShellIntent("search", "low", f"Search command via {executable}", True)
    if executable in _READ_COMMANDS:
        return ShellIntent("read", "low", f"Read/transform command via {executable}", True)
    if executable in _INSPECT_COMMANDS:
        return ShellIntent("inspect", "low", f"Inspection command via {executable}", True)
    if executable in _FILE_WRITE_COMMANDS:
        return ShellIntent("file_write", "high", f"Filesystem mutation via {executable}", False)
    if executable in _NETWORK_COMMANDS:
        return ShellIntent("network", "high", f"Network-capable command via {executable}", False)
    if executable in _PROCESS_COMMANDS:
        return ShellIntent("process_control", "high", f"Process/system control via {executable}", False)
    if executable == "git":
        return _classify_git_intent(ast)
    if executable in _PACKAGE_COMMANDS:
        return _classify_package_intent(ast)
    if executable in {"pytest", "tox", "nox"}:
        return ShellIntent("test", "low", f"Test runner via {executable}", True)
    if executable in {"make", "just"}:
        if any(token in _TEST_SUBCOMMANDS for token in lower_tokens):
            return ShellIntent("test", "low", f"Task runner test target via {executable}", True)
        return ShellIntent("build", "medium", f"Task runner invocation via {executable}", False)
    if executable in _SHELL_WRAPPERS:
        return ShellIntent("interpreter_script", "medium", f"Shell interpreter invocation via {executable}", False)
    if executable in _INLINE_EVAL_FLAGS:
        return ShellIntent("interpreter_script", "medium", f"Interpreter invocation via {executable}", False)
    if ast.has_shell_features:
        return ShellIntent("shell_features", "high", "Shell control operators or redirection", False)
    return ShellIntent("unknown", "medium", f"Unclassified command via {executable or 'unknown'}", False)


def inspect_shell_command(command: str) -> ShellCommandInspection:
    normalized = normalize_shell_command(command)
    ast = _parse_shell_ast(normalized)
    if not ast.exec_tokens:
        raise ValueError("Empty command")
    return ShellCommandInspection(
        normalized=normalized,
        ast=ast,
        intent=classify_shell_command(ast),
    )
