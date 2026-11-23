from __future__ import annotations

import json

from bokaclaw_agent.agent.protocol import contains_dsml, parse_dsml_tool_calls, strip_dsml

DSML_BASH = """<｜｜DSML｜｜ calls>
<｜｜DSML｜｜ invoke name="Bash">
<｜｜DSML｜｜ parameter name="command" string="true">cd /app/workspace && python3 - <<'PY'
print("hi")
PY</｜｜DSML｜｜ parameter>
<｜｜DSML｜｜ parameter name="description" string="true">Generate the workbook</｜｜DSML｜｜ parameter>
</｜｜DSML｜｜ invoke>
</｜｜DSML｜｜ calls>"""


def test_contains_dsml_detects_the_marker_only():
    assert contains_dsml(DSML_BASH) is True
    assert contains_dsml("plain text") is False
    assert contains_dsml("") is False
    assert contains_dsml(None) is False


def test_parse_dsml_tool_calls_recovers_name_and_string_parameters():
    calls = parse_dsml_tool_calls(DSML_BASH)

    assert len(calls) == 1
    call = calls[0]
    assert call.id.startswith("call_dsml_")
    assert call.name == "Bash"
    assert call.arguments["description"] == "Generate the workbook"
    assert 'print("hi")' in call.arguments["command"]
    assert json.loads(call.raw_arguments) == call.arguments


def test_parse_dsml_tool_calls_decodes_non_string_parameters():
    text = (
        '<｜｜DSML｜｜ invoke name="add">'
        '<｜｜DSML｜｜ parameter name="a" string="false">1</｜｜DSML｜｜ parameter>'
        '<｜｜DSML｜｜ parameter name="b" string="false">2</｜｜DSML｜｜ parameter>'
        "</｜｜DSML｜｜ invoke>"
    )

    call = parse_dsml_tool_calls(text)[0]

    assert call.arguments == {"a": 1, "b": 2}


def test_parse_dsml_tool_calls_keeps_multiple_invocations():
    second = (
        '<｜｜DSML｜｜ invoke name="Bash">'
        '<｜｜DSML｜｜ parameter name="command" string="true">ls</｜｜DSML｜｜ parameter>'
        "</｜｜DSML｜｜ invoke>"
    )

    calls = parse_dsml_tool_calls(DSML_BASH + "\n" + second)

    assert [call.name for call in calls] == ["Bash", "Bash"]
    assert calls[1].arguments == {"command": "ls"}


def test_parse_dsml_tool_calls_does_not_invent_arguments_from_malformed_markup():
    assert parse_dsml_tool_calls("<｜｜DSML｜｜ calls>\n</｜｜DSML｜｜ calls>") == []
    assert parse_dsml_tool_calls("plain text") == []


def test_string_parameters_decode_html_entities():
    text = (
        '<｜｜DSML｜｜ invoke name="Bash">'
        '<｜｜DSML｜｜ parameter name="command" string="true">true &amp;&amp; echo ok</｜｜DSML｜｜ parameter>'
        "</｜｜DSML｜｜ invoke>"
    )

    assert parse_dsml_tool_calls(text)[0].arguments["command"] == "true && echo ok"


def test_strip_dsml_removes_the_block_and_keeps_surrounding_prose():
    text = "The answer is 42.\n" + DSML_BASH + "\nEnd of answer."

    cleaned = strip_dsml(text)

    assert cleaned.startswith("The answer is 42.")
    assert cleaned.endswith("End of answer.")
    assert "<｜｜DSML｜｜" not in cleaned


def test_strip_dsml_removes_an_unclosed_block_line_by_line():
    cleaned = strip_dsml("prose\n<｜｜DSML｜｜ calls>\nmore prose")

    assert cleaned == "prose\nmore prose"
