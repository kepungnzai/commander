import asyncio
import os
import sys
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv
from google.adk.agents import LlmAgent
from google.adk.models.lite_llm import LiteLlm
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from app.prompt import ROOT_AGENT_INSTRUCTION
from app.tools.container_toolset import (
    container_image_finder_tool,
    run_container_command_tool, 
)

from app.tools.file_toolset import (
    read_file
)

load_dotenv()

model = LiteLlm(
    model="openai/empero-ai/Qwen3.8-2B-Distill-GGUF",
    api_base=os.getenv("LLM_API_BASE", "http://localhost:8888/v1"),
    api_key=os.getenv("LLM_API_KEY", "dummy"),
    # Turn off "thinking" so the small model acts instead of narrating.
    # Some servers ignore this; if so, use a non-thinking model variant.
    extra_body={"chat_template_kwargs": {"enable_thinking": False}},
    temperature=0.2,
)

root_agent = LlmAgent(
    name="commander",
    description="Agent that reads, understands and executes commands specified in a README or external HTTPS URL document.",
    model=model,
    instruction=ROOT_AGENT_INSTRUCTION,
    tools=[container_image_finder_tool, run_container_command_tool, read_file],
)

APP_NAME = "commander_app"
USER_ID = "local_user"
SESSION_ID = "session_1"

def is_url(source: str) -> bool:
    parsed = urlparse(source)
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def load_document(source: str) -> str:
    """Return the text of a README, from a URL or a local file path."""
    if is_url(source):
        req = urllib.request.Request(source, headers={"User-Agent": "commander-agent"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read().decode("utf-8", errors="replace")

    path = Path(source).expanduser()
    if not path.is_file():
        raise FileNotFoundError(f"Local file not found: {path}")
    return path.read_text(encoding="utf-8", errors="replace")


async def run(source: str) -> None:
    try:
        content = load_document(source)
    except Exception as e:
        print(f"Error loading '{source}': {e}")
        sys.exit(1)

    session_service = InMemorySessionService()
    await session_service.create_session(
        app_name=APP_NAME, user_id=USER_ID, session_id=SESSION_ID
    )
    runner = Runner(
        agent=root_agent, app_name=APP_NAME, session_service=session_service
    )

    message = types.Content(
        role="user",
        parts=[
            types.Part(
                text=(
                    f"Here is the README (source: {source}). "
                    f"Execute its instructions using your tools.\n\n"
                    f"--- README START ---\n{content}\n--- README END ---"
                )
            )
        ],
    )

    final_text = None

    async for event in runner.run_async(
        user_id=USER_ID, session_id=SESSION_ID, new_message=message
    ):
        print(f"--- event from {event.author} | final={event.is_final_response()}")

        if event.content and event.content.parts:
            for part in event.content.parts:
                if part.text:
                    print(f"[text] {part.text[:500]}")
                if part.function_call:
                    print(
                        f"[tool call] {part.function_call.name}"
                        f"({part.function_call.args})"
                    )
                if part.function_response:
                    print(
                        f"[tool result] {str(part.function_response.response)[:500]}"
                    )

        if event.is_final_response() and event.content and event.content.parts:
            final_text = "".join(p.text or "" for p in event.content.parts)

    print("\n=== Final answer ===")
    print(final_text or "(no final answer)")


def main() -> None:
    if len(sys.argv) != 2:
        print("Usage: python agent.py <url-or-local-path-to-readme.md>")
        sys.exit(1)
    asyncio.run(run(sys.argv[1]))


if __name__ == "__main__":
    main()