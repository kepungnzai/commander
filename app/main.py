import json
import os
import re
import sys
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

import litellm
from dotenv import load_dotenv

from app.tools.container_toolset import (
    start_container,
    run_container_command,
    stop_container,
)

load_dotenv()

MODEL = "openai/empero-ai/Qwen3.8-2B-Distill-GGUF"
API_BASE = os.getenv("LLM_API_BASE", "http://localhost:8888/v1")
API_KEY = os.getenv("LLM_API_KEY", "dummy")
MAX_STEPS = 30

litellm.suppress_debug_info = True
litellm.set_verbose = False

DENYLIST = [
    r"\brm\s+-rf\s+/(\s|$)",
    r"\bmkfs\b",
    r"\bdd\s+if=",
    r":\(\)\s*\{",
    r"(curl|wget)[^|]*\|\s*(sudo\s+)?(ba)?sh",
    r"\bshutdown\b|\breboot\b",
]

SYSTEM_PROMPT = """You are a command execution agent. You are given a README.
Each turn you choose ONE action and reply with JSON only: {"action": ..., "argument": ...}

Actions:
- start_container: argument is an image name, e.g. python:3.12-slim. Must be the first action.
- run_container_command: argument is ONE shell command to run in the container.
- finish: argument is a short summary of what ran, the real results, and how to verify.

Rules:
- Pick an image that fits the README (python:3.12-slim, node:22-slim, ubuntu:24.04).
- Run the README's commands in order, exactly as written. Install missing tools first
  (e.g. apt-get update && apt-get install -y git).
- Each command runs in a fresh shell, so chain directory changes with && on one line.
- After each action you will receive the real result. Never invent results.
- If a command fails, read the error and fix it, at most 2 retries.
- If a command is harmful, do not run it; use finish and explain why.
- When all commands are done, use finish.
"""

SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string",
                   "enum": ["start_container", "run_container_command", "finish"]},
        "argument": {"type": "string"},
    },
    "required": ["action", "argument"],
    "additionalProperties": False,
}


def is_url(s: str) -> bool:
    p = urlparse(s)
    return p.scheme in ("http", "https") and bool(p.netloc)


def load_document(source: str) -> str:
    if is_url(source):
        req = urllib.request.Request(source, headers={"User-Agent": "commander-agent"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.read().decode("utf-8", errors="replace")
    path = Path(source).expanduser()
    if not path.is_file():
        raise FileNotFoundError(f"Local file not found: {path}")
    return path.read_text(encoding="utf-8", errors="replace")


def execute(action: str, argument: str) -> dict:
    if action == "start_container":
        return start_container(argument)
    if action == "run_container_command":
        if any(re.search(p, argument) for p in DENYLIST):
            return {"status": "error", "error_message": f"Blocked by safety policy: {argument}"}
        return run_container_command(argument)
    return {"status": "error", "error_message": f"Unknown action: {action}"}


def ask_model(messages) -> dict:
    resp = litellm.completion(
        model=MODEL, api_base=API_BASE, api_key=API_KEY,
        messages=messages, temperature=0.2, max_tokens=800,
        response_format={
            "type": "json_schema",
            "json_schema": {"name": "agent_action", "schema": SCHEMA, "strict": True},
        },
        extra_body={"chat_template_kwargs": {"enable_thinking": False}},
    )
    raw = resp.choices[0].message.content or ""
    print(f"    [raw model output] {raw[:300]}")
    # tolerate stray text around the JSON
    m = re.search(r"\{.*\}", raw, re.S)
    return json.loads(m.group(0)) if m else {}


def run(source: str) -> None:
    try:
        readme = load_document(source)
    except Exception as e:
        print(f"Error loading '{source}': {e}")
        sys.exit(1)

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content":
            f"README (source: {source}):\n--- README START ---\n{readme}\n--- README END ---\n\n"
            f"Choose your first action."},
    ]

    final = None
    container_started = False
    try:
        for step in range(1, MAX_STEPS + 1):
            try:
                decision = ask_model(messages)
            except Exception as e:
                print(f"[step {step}] model/JSON error: {e}")
                messages.append({"role": "user", "content":
                    'Reply with valid JSON only: {"action": ..., "argument": ...}'})
                continue

            action = decision.get("action", "")
            argument = str(decision.get("argument", ""))
            print(f"\n[step {step}] {action}: {argument}")
            messages.append({"role": "assistant", "content": json.dumps(decision)})

            if action == "finish":
                final = argument
                break

            if action == "run_container_command" and not container_started:
                result = {"status": "error",
                          "error_message": "No container yet. Use start_container first."}
            else:
                result = execute(action, argument)
                if action == "start_container" and result.get("status") == "success":
                    container_started = True

            print(f"[result] {json.dumps(result)[:800]}")
            messages.append({"role": "user", "content":
                f"Result of {action}: {json.dumps(result)}\nChoose your next action."})
    finally:
        stop_container()

    print("\n=== Final answer ===")
    print(final or "(agent did not finish)")


def main() -> None:
    if len(sys.argv) != 2:
        print("Usage: python agent.py <url-or-local-path-to-readme.md>")
        sys.exit(1)
    run(sys.argv[1])


if __name__ == "__main__":
    main()