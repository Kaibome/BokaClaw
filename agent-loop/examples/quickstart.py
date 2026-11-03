"""Run a demo agent with a scripted provider and in-memory sessions.

No API key or database is required. Swap ``DemoProvider`` for the built-in
``OpenAICompatibleProvider`` (or set ``AgentConfig.model``) and pass a
``PostgresSessionStore`` to persist real conversations.
"""

from bokaclaw_agent import (
    AgentConfig,
    AgentRuntime,
    InMemorySessionStore,
    ModelResponse,
    ToolCall,
    tool,
)


@tool
def get_weather(city: str) -> str:
    """Return the current weather for a city.

    Args:
        city: The city to look up.
    """
    return f"{city}: sunny, 25C"


class DemoProvider:
    """First turn asks for a tool call, second turn returns the answer."""

    def __init__(self) -> None:
        self.turn = 0

    def complete(self, messages, *, tools=None, tool_choice="auto", **kwargs):
        self.turn += 1
        if self.turn == 1:
            return ModelResponse(
                content=None,
                finish_reason="tool_calls",
                tool_calls=[
                    ToolCall(
                        id="call_weather",
                        name="get_weather",
                        arguments={"city": "Beijing"},
                        raw_arguments='{"city": "Beijing"}',
                    )
                ],
            )
        return ModelResponse(content="Beijing is sunny at 25C.", finish_reason="stop")


def main() -> None:
    store = InMemorySessionStore()
    runtime = AgentRuntime(
        AgentConfig(name="weather-agent", system_prompt="Use tools when helpful.", max_iterations=4),
        provider=DemoProvider(),
        tools=[get_weather],
        store=store,
    )

    result = runtime.run(
        "What's the weather in Beijing?",
        session_id="demo-session",
        user_id="demo-user",
    )

    print("answer:", result.content)
    print("iterations:", result.iterations)
    print("tool results:", [item.content for item in result.tool_results])
    print("session:", runtime.get_session("demo-session"))


if __name__ == "__main__":
    main()
