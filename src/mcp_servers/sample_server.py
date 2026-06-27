"""
サンプル MCP サーバー

sessions_spawn_dynamic でのテスト用。
stdio / SSE どちらの接続にも対応。

ツール一覧:
  - echo          : テキストをそのまま返す
  - add           : 2つの数値を加算する
  - current_time  : 現在の日時を返す
  - reverse_text  : テキストを逆順にして返す

起動方法:
  # stdio モード (boiled-claw から sessions_spawn_dynamic で使う場合)
  python -m src.mcp_servers.sample_server

  # SSE モード (HTTP 接続テスト用)
  python -m src.mcp_servers.sample_server --sse --port 8765
"""

import argparse
import datetime


def create_server(host: str = "127.0.0.1", port: int = 8765):
    from mcp.server.fastmcp import FastMCP
    from mcp.server.fastmcp.server import TransportSecuritySettings

    mcp = FastMCP("sample-tools")
    mcp.settings.host = host
    mcp.settings.port = port
    # Docker ネットワーク内のコンテナ名でのアクセスを許可する
    mcp.settings.transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=False,
    )

    @mcp.tool()
    def echo(text: str) -> str:
        """入力されたテキストをそのまま返す。接続テスト用。"""
        return f"echo: {text}"

    @mcp.tool()
    def add(a: float, b: float) -> str:
        """2つの数値を加算して結果を返す。"""
        result = a + b
        return f"{a} + {b} = {result}"

    @mcp.tool()
    def current_time() -> str:
        """現在の日時を ISO 8601 形式で返す。"""
        return datetime.datetime.now().isoformat()

    @mcp.tool()
    def reverse_text(text: str) -> str:
        """テキストを逆順にして返す。"""
        return text[::-1]

    return mcp


def main():
    parser = argparse.ArgumentParser(description="Sample MCP Server")
    parser.add_argument("--sse", action="store_true", help="SSE モードで起動")
    parser.add_argument("--port", type=int, default=8765, help="SSE モードのポート番号")
    parser.add_argument("--host", type=str, default="127.0.0.1", help="SSE モードのホスト")
    args = parser.parse_args()

    if args.sse:
        mcp = create_server(host=args.host, port=args.port)
        print(f"SSE モードで起動: http://{args.host}:{args.port}/sse")
        mcp.run(transport="sse")
    else:
        mcp = create_server()
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
