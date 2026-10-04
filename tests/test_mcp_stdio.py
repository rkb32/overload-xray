"""The way an agent really launches the server: a separate process speaking MCP over stdin/stdout."""
import asyncio
import os
import sys

from mcp import Client, StdioServerParameters

from test_mcp import is_error, text_of, write_readme_example

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_the_server_works_when_launched_as_a_subprocess_over_stdio(tmp_path):
    write_readme_example(tmp_path / "run")
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "xray", "mcp"],
        cwd=ROOT,
        env={**os.environ, "XRAY_ROOT": str(tmp_path)},
    )

    async def go():
        async with Client(params) as client:
            tools = {tool.name for tool in (await client.list_tools()).tools}
            ok = await client.call_tool("diagnose", {"path": "run"})
            refused = await client.call_tool("report", {"path": ".."})
            return tools, ok, refused

    tools, ok, refused = asyncio.run(go())

    assert tools >= {"report", "diagnose", "compare"}
    assert not is_error(ok) and "[zombie_work]" in text_of(ok)
    assert is_error(refused) and "outside" in text_of(refused)
