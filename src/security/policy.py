"""
セキュリティポリシー
コマンド実行やファイルアクセスの制御
"""

from typing import List, Optional
from pathlib import Path
import re

from src.security.shell_intent import ShellCommandInspection, inspect_shell_command


class SecurityPolicy:
    """セキュリティポリシー管理"""

    def __init__(
        self,
        allowed_commands: Optional[List[str]] = None,
        blocked_commands: Optional[List[str]] = None,
        allowed_paths: Optional[List[str]] = None,
        blocked_paths: Optional[List[str]] = None,
    ):
        self.allowed_commands = allowed_commands or []
        self.blocked_commands = blocked_commands or self._default_blocked_commands()
        self.allowed_paths = [Path(p).resolve() for p in (allowed_paths or [])]
        self.blocked_paths = [Path(p).resolve() for p in (blocked_paths or self._default_blocked_paths())]

    @staticmethod
    def _default_blocked_commands() -> List[str]:
        """デフォルトのブロックコマンド"""
        return [
            "rm -rf",
            "sudo rm",
            "mkfs",
            "dd if=",
            "> /dev/",
            "chmod 777",
            "chmod -R 777",
            ":(){ :|:& };:",  # fork bomb
            "wget http",  # 外部ダウンロード制限
            "curl http",
            "nc -l",  # netcat リスナー
            "python -m http.server",
            "python -m SimpleHTTPServer",
        ]

    @staticmethod
    def _default_blocked_paths() -> List[str]:
        """デフォルトのブロックパス"""
        return [
            "/etc/passwd",
            "/etc/shadow",
            "/etc/sudoers",
            "/root",
            "/boot",
            "/sys",
            "/proc",
            "~/.ssh/id_rsa",
            "~/.ssh/id_ed25519",
        ]

    def is_command_allowed(
        self,
        command: str,
        inspection: Optional[ShellCommandInspection] = None,
    ) -> tuple[bool, Optional[str]]:
        """
        コマンドが許可されているかチェック

        Returns:
            (許可/拒否, 理由)
        """
        if inspection is None:
            try:
                inspection = inspect_shell_command(command)
            except ValueError as exc:
                return False, f"Invalid command syntax: {exc}"

        validation_error = inspection.validation_error()
        if validation_error:
            return False, validation_error

        # ブロックコマンドのチェック
        for blocked in self.blocked_commands:
            if blocked in command:
                return False, f"Blocked command pattern detected: '{blocked}'"

        # 許可リストが設定されている場合はチェック
        if self.allowed_commands:
            for allowed in self.allowed_commands:
                if command.startswith(allowed):
                    return True, None
            return False, "Command not in allowed list"

        return True, None

    def is_path_allowed(self, path: str, operation: str = "read") -> tuple[bool, Optional[str]]:
        """
        パスアクセスが許可されているかチェック

        Args:
            path: アクセスパス
            operation: 操作タイプ ('read' or 'write')

        Returns:
            (許可/拒否, 理由)
        """
        try:
            resolved_path = Path(path).expanduser().resolve()
        except Exception as e:
            return False, f"Invalid path: {e}"

        # ブロックパスのチェック
        for blocked in self.blocked_paths:
            try:
                if resolved_path == blocked or blocked in resolved_path.parents:
                    return False, f"Access to '{blocked}' is blocked"
            except Exception:
                continue

        # 許可リストが設定されている場合はチェック
        if self.allowed_paths:
            for allowed in self.allowed_paths:
                try:
                    if resolved_path == allowed or allowed in resolved_path.parents:
                        return True, None
                except Exception:
                    continue
            return False, "Path not in allowed list"

        # 書き込み操作の追加チェック
        if operation == "write":
            # システムディレクトリへの書き込み防止
            system_dirs = ["/bin", "/sbin", "/usr/bin", "/usr/sbin", "/lib", "/lib64"]
            for sys_dir in system_dirs:
                sys_path = Path(sys_dir)
                try:
                    if sys_path in resolved_path.parents:
                        return False, f"Write to system directory '{sys_dir}' is blocked"
                except Exception:
                    continue

        return True, None

    def validate_file_content(self, content: str, path: str) -> tuple[bool, Optional[str]]:
        """
        ファイル内容の検証

        Args:
            content: ファイル内容
            path: ファイルパス

        Returns:
            (許可/拒否, 理由)
        """
        # 秘密鍵やトークンのパターン検出
        sensitive_patterns = [
            (r"-----BEGIN [A-Z]+ PRIVATE KEY-----", "Private key detected"),
            (r"[A-Za-z0-9]{32,}", "Potential API key or token detected"),
            (r"password\s*=\s*['\"][^'\"]+['\"]", "Password detected in plaintext"),
        ]

        for pattern, reason in sensitive_patterns:
            if re.search(pattern, content, re.IGNORECASE):
                # .envファイルなど、意図的な設定ファイルは除外
                if not (path.endswith(".env") or path.endswith(".env.example")):
                    return False, reason

        return True, None


# グローバルインスタンス
_security_policy: Optional[SecurityPolicy] = None


def get_security_policy() -> SecurityPolicy:
    """セキュリティポリシーインスタンスを取得"""
    global _security_policy
    if _security_policy is None:
        from src.config.settings import get_settings
        settings = get_settings()
        allowed_paths = [
            p.strip() for p in settings.file_workspace_paths.split(",") if p.strip()
        ]
        _security_policy = SecurityPolicy(allowed_paths=allowed_paths or None)
    return _security_policy
