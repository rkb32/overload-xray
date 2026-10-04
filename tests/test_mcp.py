import asyncio
import json

from mcp import Client

from xray.mcp_server import server

BASE = "2026-10-03T12:00:{:06.3f}Z"


def sdk_span(span_id, parent, service, kind, start, end, error=False, description=None, http=None):
    """One span in the OpenTelemetry Python SDK's JSON shape, offsets in seconds from a fixed base."""
    attributes = {} if http is None else {"http.status_code": http}
    status = {"status_code": "ERROR", **({"description": description} if description else {})} if error else {"status_code": "UNSET"}
    return json.dumps({
        "name": f"{service}-{kind}",
        "context": {"trace_id": "0xabc", "span_id": span_id, "trace_state": "[]"},
        "kind": f"SpanKind.{kind}",
        "parent_id": parent,
        "start_time": BASE.format(start),
        "end_time": BASE.format(end),
        "status": status,
        "attributes": attributes,
        "events": [],
        "links": [],
        "resource": {"attributes": {"service.name": service}, "schema_url": ""},
    })


def write_readme_example(folder):
    """The README example: attempt 1 times out at 1s while B works until 5s; attempt 2 succeeds at 6s."""
    folder.mkdir(parents=True, exist_ok=True)
    lines = [
        sdk_span("0xa", None, "A", "SERVER", 0, 6),
        sdk_span("0xc1", "0xa", "A", "CLIENT", 0, 1, error=True, description="ReadTimeout: "),
        sdk_span("0xb1", "0xc1", "B", "SERVER", 0, 5),
        sdk_span("0xc2", "0xa", "A", "CLIENT", 1, 6, http=200),
        sdk_span("0xb2", "0xc2", "B", "SERVER", 1, 6),
    ]
    (folder / "spans.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")


def call(tool, arguments):
    async def go():
        async with Client(server) as client:
            return await client.call_tool(tool, arguments)

    return asyncio.run(go())


def text_of(result) -> str:
    return "\n".join(part.text for part in result.content if hasattr(part, "text"))


def is_error(result) -> bool:
    return bool(getattr(result, "is_error", getattr(result, "isError", False)))


def test_the_server_offers_its_tools():
    async def go():
        async with Client(server) as client:
            return {tool.name for tool in (await client.list_tools()).tools}

    assert asyncio.run(go()) >= {"report", "diagnose", "compare", "retries"}


def test_report_gives_the_numbers(monkeypatch, tmp_path):
    write_readme_example(tmp_path / "run")
    monkeypatch.setenv("XRAY_ROOT", str(tmp_path))

    result = call("report", {"path": "run"})

    assert not is_error(result)
    assert "A -> B" in text_of(result) and "50%" in text_of(result)


def test_diagnose_names_the_finding_and_the_fix(monkeypatch, tmp_path):
    write_readme_example(tmp_path / "run")
    monkeypatch.setenv("XRAY_ROOT", str(tmp_path))

    text = text_of(call("diagnose", {"path": "run"}))

    assert "[zombie_work]" in text and "deadline propagation" in text


def test_compare_two_runs(monkeypatch, tmp_path):
    write_readme_example(tmp_path / "before")
    write_readme_example(tmp_path / "after")
    monkeypatch.setenv("XRAY_ROOT", str(tmp_path))

    assert "before" in text_of(call("compare", {"before": "before", "after": "after"}))


def test_a_path_that_climbs_out_of_the_root_is_refused(monkeypatch, tmp_path):
    root = tmp_path / "root"
    write_readme_example(tmp_path / "outside")
    root.mkdir()
    monkeypatch.setenv("XRAY_ROOT", str(root))

    result = call("report", {"path": "../outside"})

    assert is_error(result) and "outside" in text_of(result)


def test_an_absolute_path_outside_the_root_is_refused(monkeypatch, tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    write_readme_example(tmp_path / "elsewhere")
    monkeypatch.setenv("XRAY_ROOT", str(root))

    result = call("report", {"path": str(tmp_path / "elsewhere")})

    assert is_error(result) and "outside" in text_of(result)


def test_a_file_that_is_not_spans_is_a_clear_error(monkeypatch, tmp_path):
    (tmp_path / "run").mkdir()
    (tmp_path / "run" / "broken.jsonl").write_text("this is not json\n", encoding="utf-8")
    monkeypatch.setenv("XRAY_ROOT", str(tmp_path))

    result = call("report", {"path": "run"})

    assert is_error(result) and "not valid JSON" in text_of(result)


def test_asking_for_jev_without_a_key_is_a_clear_error(monkeypatch, tmp_path):
    write_readme_example(tmp_path / "run")
    monkeypatch.setenv("XRAY_ROOT", str(tmp_path))
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)

    result = call("report", {"path": "run", "use_jev": True})

    assert is_error(result) and "API key" in text_of(result)


def test_retries_gives_the_retry_map(monkeypatch, tmp_path):
    write_readme_example(tmp_path / "run")
    monkeypatch.setenv("XRAY_ROOT", str(tmp_path))

    result = call("retries", {"path": "run"})

    assert not is_error(result)
    assert "1 call made 2 attempts" in text_of(result) and "50%" in text_of(result)


def test_retries_refuses_a_path_outside_the_root_like_the_other_tools(monkeypatch, tmp_path):
    (tmp_path / "root").mkdir()
    monkeypatch.setenv("XRAY_ROOT", str(tmp_path / "root"))

    refused = call("retries", {"path": ".."})

    assert is_error(refused) and "outside the allowed folder" in text_of(refused)  # refused by the path check, not "no such tool"
