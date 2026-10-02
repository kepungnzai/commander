"""
Single-file test: Google ADK + a local OpenAI-compatible server (Qwen) calling tools.
 
Run:  python adk_tool_test.py
Optional: set LOCAL_API_KEY in your environment if your server needs a real key.
"""
import asyncio
import os
 
from google.adk.agents import Agent
from google.adk.models.lite_llm import LiteLlm
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

# ---------- Tools (plain Python functions) ----------
def get_weather(city: str) -> dict:
    """Returns the current weather for a city.
 
    Args:
        city: The name of the city, e.g. "Melbourne".
    """
    return {"city": city, "temp_c": 17, "condition": "cloudy"}
 
 
def add_numbers(a: float, b: float) -> dict:
    """Adds two numbers together.
 
    Args:
        a: The first number.
        b: The second number.
    """
    return {"result": a + b}

# ---------- Model + Agent ----------
model = LiteLlm(
    model="openai/empero-ai/Qwen3.8-2B-Distill-GGUF",
    api_base="http://localhost:8888/v1",
    api_key=os.environ.get("LOCAL_API_KEY", "sk-local"),
    temperature=2,
)
 
agent = Agent(
    name="tool_agent",
    model=model,
    # NOTE: instruction goes on the Agent, not on LiteLlm
    instruction=(
        "You are a helpful assistant. "
        "Use get_weather for weather questions and add_numbers for addition. "
        "Never guess these values yourself; always call the tool. "
        "Call one tool at a time and wait for its result before continuing."
    ),
    tools=[get_weather, add_numbers],
)

# ---------- Runner: prints every step so you can see the tool call ----------
async def ask(question: str, session_id: str):
    print(f"\n=== USER: {question}")
    session_service = InMemorySessionService()
    await session_service.create_session(
        app_name="demo", user_id="u1", session_id=session_id
    )
    runner = Runner(agent=agent, app_name="demo", session_service=session_service)
 
    message = types.Content(role="user", parts=[types.Part(text=question)])
 
    async for event in runner.run_async(
        user_id="u1", session_id=session_id, new_message=message
    ):
        if not event.content or not event.content.parts:
            continue
        for part in event.content.parts:
            if part.function_call:
                print(f"MODEL CALLS TOOL: {part.function_call.name}({dict(part.function_call.args)})")
            elif part.function_response:
                print(f"TOOL RETURNED:    {part.function_response.response}")
            elif part.text and event.is_final_response():
                print(f"FINAL ANSWER:     {part.text}")
 
async def main():
    await ask("What's the weather in Melbourne?", "s1")   # one tool
    await ask("What is 12.5 + 7.5?", "s2")                # other tool
    await ask("What's the weather in Melbourne, and what is 12.5 + 7.5?", "s3")  # both
 
if __name__ == "__main__":
    asyncio.run(main())