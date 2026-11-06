"""Run one subagent delegation with scripted providers.

No API key or database is required. The parent model asks the ``explore``
subagent to inspect a file; the child runs its own ReAct loop and only the
structured conclusion reaches the parent context.
"""

from bokaclaw_agent import (
    AgentConfig,
    AgentRuntime,
    ModelConfig,
    ModelResponse,
    SubagentOptions,
    SubagentSpec,
    ToolCall,
    tool,
)


@tool(name="Read")
def read_file(path: str) -> str:
    """Read a workspace file.

    Args:
        path: File path to read.
    """
    return "def greet(name):\n    return f'Hello, {name}!'\n"


class ParentProvider:
    """First turn delegates; second turn answers from the child's result."""

    def __init__(self) -> None:
        self.turn = 0
        self.child_result = ""

    def complete(self, messages, *, tools=None, tool_choice="auto", **kwargs):
        self.turn += 1
        if self.turn == 1:
            return ModelResponse(
                content=None,
                finish_reason="tool_calls",
                tool_calls=[
                    ToolCall(
                        id="call_explore",
                        name="explore",
                        arguments={
                            "goal": "Inspect greet.py and identify the function it defines.",
                            "label": "inspect greet.py",
                            "scope": ["/workspace/greet.py"],
                            "background": "You cannot see the parent conversation.",
                            "questions": ["Which function is defined?"],
                            "output_format": "JSON object",
                        },
                    )
                ],
            )
        self.child_result = next(message["content"] for message in reversed(messages) if message.get("role") == "tool")
        return ModelResponse(content="greet.py defines greet(name).", finish_reason="stop")


class ChildProvider:
    """Read the scoped file, then return the structured envelope."""

    def __init__(self) -> None:
        self.turn = 0

    def complete(self, messages, *, tools=None, tool_choice="auto", **kwargs):
        self.turn += 1
        if self.turn == 1:
            return ModelResponse(
                content=None,
                finish_reason="tool_calls",
                tool_calls=[ToolCall(id="call_read", name="Read", arguments={"path": "/workspace/greet.py"})],
            )
        return ModelResponse(
            content=(
                '{"report": "greet.py defines greet(name).", '
                '"facts": [{"claim": "greet(name) is defined", "evidence": "greet.py:1", '
                '"confidence": "high"}], '
                '"sources": ["/workspace/greet.py"]}'
            ),
            finish_reason="stop",
        )


def main() -> None:
    parent = ParentProvider()
    runtime = AgentRuntime(
        AgentConfig(
            name="parent",
            system_prompt="Delegate noisy exploration to subagents.",
            max_iterations=4,
        ),
        provider=parent,
        tools=[read_file],
        subagents=[
            SubagentSpec(
                name="explore",
                description="Read-only file inspection. Use when only a short conclusion is needed.",
                system_prompt="You explore files and report evidence. You cannot modify files.",
                tool_policy="read_only",
                model=ModelConfig(model="demo-child"),
            )
        ],
        subagent_options=SubagentOptions(max_calls_per_run=2),
        subagent_provider_factory=lambda _model: ChildProvider(),
    )

    result = runtime.run("What does greet.py define?", session_id="demo-subagent")

    print("answer:", result.content)
    print("child result:", parent.child_result)


if __name__ == "__main__":
    main()
