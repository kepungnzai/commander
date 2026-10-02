import asyncio
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types
from agent import root_agent

async def main():
    session_service = InMemorySessionService()
    await session_service.create_session(
        app_name="demo", user_id="u1", session_id="s1"
    )
    runner = Runner(
        agent=root_agent, app_name="demo", session_service=session_service
    )

    message = types.Content(
        role="user",
        parts=[types.Part(text="What's the weather in Melbourne, and what is 12.5 + 7.5?")],
    )

    async for event in runner.run_async(
        user_id="u1", session_id="s1", new_message=message
    ):
        if not event.content:
            continue
        for part in event.content.parts:
            if part.function_call:
                print(f"MODEL CALLS TOOL: {part.function_call.name}({dict(part.function_call.args)})")
            elif part.function_response:
                print(f"TOOL RETURNED:    {part.function_response.response}")
            elif part.text and event.is_final_response():
                print(f"FINAL ANSWER:     {part.text}")

asyncio.run(main())