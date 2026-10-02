# # app/agent.py

# """ Command Execution Agent: read understand and execute command provided in a locally clone README and external HTTPS URL."""

import os
from dotenv import load_dotenv
from google.adk.agents import LlmAgent
from google.adk.models.lite_llm import LiteLlm
from google.adk.tools import google_search

# Create a LiteLLM model pointing to your local server
model = LiteLlm(
    model="openai/empero-ai/Qwen3.8-2B-Distill-GGUF",
    api_base="http://localhost:8888/v1",  # Your local server
    api_key="sk-unsloth-4d0a1b198bd177a2a72ee1954585342a" ,
    temperature=0.0
)

from app.prompt import ROOT_AGENT_INSTRUCTION
from app.tools.container_toolset import (
    container_image_finder_tool, run_container_command_tool
)

load_dotenv()

root_agent = LlmAgent(
    name="commander",
    description="Agent that read, understand and execute commands specified in a README or external HTTPS URL document.",
    model=model,
    instruction="You are a command execution agent. You are given a README.",
    tools=[container_image_finder_tool, run_container_command_tool]
)
