"""Have the model READ a README, detect its runtime, and extract the commands.
Then start a matching container, run each command in it, and stop it.

Usage:
    python run_agent.py path/to/README.md
"""

import argparse
import asyncio
import inspect
import json
import re
import shlex
from pathlib import Path

from dotenv import load_dotenv
from google.adk.agents import LlmAgent
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types
from pydantic import BaseModel, Field

load_dotenv()

# Reuse the same model your commander agent uses.
from app.agent import root_agent  

# ASSUMPTION: plain functions behind your container tools. start_container
# takes an `image` argument (confirmed by your traceback); the others are
# guesses, so adjust to match app/tools/container_toolset.py.
from app.tools.container_toolset import ( 
    start_container,
    run_container_command,
    stop_container,
)

APP_NAME = "readme_extractor"
USER_ID = "local_user"
SESSION_ID = "session_1"

# Runtime detected from the README -> container image to use.
# Full (non-slim) images are used because they include git, which READMEs
# often need for `git clone`.
IMAGE_MAP = {
    "python": "python:3.12",
    "node": "node:22",
    "go": "golang:1.23",
    "rust": "rust:1",
    "java": "eclipse-temurin:21",
    "ruby": "ruby:3.3",
    "dotnet": "mcr.microsoft.com/dotnet/sdk:8.0",
    "shell": "ubuntu:24.04",
    "unknown": "ubuntu:24.04",
}

# Alternate names the model might return -> canonical runtime key.
RUNTIME_ALIASES = {
    "py": "python", "python3": "python", "pip": "python",
    "javascript": "node", "js": "node", "nodejs": "node", "node.js": "node",
    "typescript": "node", "ts": "node", "npm": "node",
    "golang": "go",
    "cargo": "rust",
    "c#": "dotnet", ".net": "dotnet", "csharp": "dotnet",
    "bash": "shell", "sh": "shell",
}

# Fallback: guess the runtime from the first word of each command.
COMMAND_HINTS = {
    "pip": "python", "pip3": "python", "python": "python", "python3": "python",
    "uv": "python", "poetry": "python",
    "npm": "node", "npx": "node", "yarn": "node", "pnpm": "node", "node": "node",
    "go": "go",
    "cargo": "rust", "rustc": "rust",
    "mvn": "java", "gradle": "java", "java": "java",
    "gem": "ruby", "bundle": "ruby", "ruby": "ruby",
    "dotnet": "dotnet",
}

async def call(func, *args, **kwargs):
    """Call a tool function whether it is sync or async."""
    result = func(*args, **kwargs)
    if inspect.isawaitable(result):
        result = await result
    return result


async def run_in_container(command: str, program: str, program_args: list[str]):
    """Call run_container_command, adapting to its signature.

    Your function accepts only 1 positional argument, so by default the full
    command string is passed as that single argument. If it also exposes a
    keyword parameter for arguments (args/arguments/argv), the program name goes
    in the positional slot and the arguments go in by keyword.
    """
    params = inspect.signature(run_container_command).parameters
    for name in ("args", "arguments", "argv"):
        if name in params and params[name].kind != inspect.Parameter.POSITIONAL_ONLY:
            return await call(run_container_command, program, **{name: program_args})
    return await call(run_container_command, command)


class ReadmeAnalysis(BaseModel):
    runtime: str = Field(
        description=(
            "The main language/runtime the README's commands need. One of: "
            "python, node, go, rust, java, ruby, dotnet, shell, unknown."
        )
    )
    commands: list[str] = Field(
        description="Shell commands from the README, in the order they should be run."
    )

# Dedicated extractor agent: no tools, so ADK can enforce the output schema.
extractor_agent = LlmAgent(
    name="readme_extractor",
    description="Reads a README, detects its runtime, and extracts its shell commands.",
    model=root_agent.model,
    instruction=(
        "You will receive a README between <start> and </end> tags. "
        "Read it carefully from start to finish. And do two things:\n"
        "1. Decide which runtime use, based on the "
        "install and run commands and any files mentioned (requirements.txt, "
        "package.json, go.mod, Cargo.toml, pom.xml, Gemfile, .csproj, etc.). "
        "Answer with exactly one of: python, node, go, rust, java, ruby, "
        "dotnet, shell, unknown.\n"
        "2. Extract every shell command a user is expected to run, in order "
        "of appearance. Return only the commands themselves: no prompts like "
        "'$', no comments, no explanations.\n"
        "Never execute anything."
        "3. Return in JSON format in this format: {\"runtime\": \"<runtime>\", \"commands\": [<commands>]}."
    ),
    output_schema=ReadmeAnalysis,
)

def normalize_runtime(raw: str, commands: list[str]) -> str:
    """Map the model's answer to a known runtime, inferring from commands if needed."""
    key = (raw or "").strip().lower()
    key = RUNTIME_ALIASES.get(key, key)
    if key in IMAGE_MAP and key != "unknown":
        return key

    # Model said unknown/garbage: guess from the commands instead.
    for command in commands:
        first = command.split(maxsplit=1)[0].lower() if command.split() else ""
        if first in COMMAND_HINTS:
            return COMMAND_HINTS[first]
    return "unknown"


def parse_analysis(text: str) -> tuple[str, list[str]]:
    """Parse the model reply into (runtime, commands); tolerant of messy output."""
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.MULTILINE).strip()

    runtime, commands = "", []
    try:
        analysis = ReadmeAnalysis.model_validate_json(text)
        runtime, commands = analysis.runtime, analysis.commands
    except Exception:
        for candidate in (text, *re.findall(r"\{.*\}|\[.*\]", text, flags=re.DOTALL)):
            try:
                data = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict):
                runtime = str(data.get("runtime", ""))
                data = data.get("commands", [])
            if isinstance(data, list):
                commands = [str(c).strip() for c in data if str(c).strip()]
                break
        else:
            commands = [line.strip() for line in text.splitlines() if line.strip()]

    commands = [c.strip() for c in commands if c.strip()]
    return normalize_runtime(runtime, commands), commands


async def analyze_readme(readme_text: str) -> tuple[str, list[str]]:
    session_service = InMemorySessionService()
    runner = Runner(
        agent=extractor_agent,
        app_name=APP_NAME,
        session_service=session_service,
    )
    await session_service.create_session(
        app_name=APP_NAME, user_id=USER_ID, session_id=SESSION_ID
    )

    message = types.Content(
        role="user",
        parts=[types.Part(text=f"<start>\n{readme_text}\n</end>")],
    )

    final_text = ""
    async for event in runner.run_async(
        user_id=USER_ID, session_id=SESSION_ID, new_message=message
    ):
        if event.is_final_response() and event.content and event.content.parts:
            final_text = "".join(p.text or "" for p in event.content.parts)

    print(final_text)
    return parse_analysis(final_text)


async def main() -> None:
    parser = argparse.ArgumentParser(description="Run the commands in a README.")
    parser.add_argument("readme", type=Path, help="Path to the README file")
    args = parser.parse_args()

    if not args.readme.is_file():
        parser.error(f"File not found: {args.readme}")

    runtime, commands = await analyze_readme(args.readme.read_text(encoding="utf-8"))
    image = IMAGE_MAP[runtime]

    print(f"Detected runtime: {runtime}  ->  image: {image}")
    print(f"Found {len(commands)} command(s):\n")

    if len(commands) == 0:
        print("Nothing to run.")
        return

    print(f"Starting container ({image})...")
    await call(start_container, image=image)

    try:
        for i, command in enumerate(commands, start=1):
            try:
                argv = shlex.split(command)
            except ValueError:
                argv = [command]
            if not argv:
                continue

            program, program_args = argv[0], argv[1:]
            print(f"\n{i}. {command}")
            print(f"   command: {program!r}  args: {program_args}")

            result = await run_in_container(command, program, program_args)
            print(f"   result: {result}")
    finally:
        # Always clean up, even if a command raises.
        print("\nStopping container...")
        await call(stop_container)


if __name__ == "__main__":
    asyncio.run(main())