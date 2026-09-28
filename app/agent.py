# app/agent.py

""" Command Execution Agent: read understand and execute command provided in a locally clone README and external HTTPS URL."""

import os
from dotenv import load_dotenv
from google.adk.agents import LlmAgent

from app.prompt import ROOT_AGENT_INSTRUCTION
from app.tools.document_toolset import (
    container_image_registry,
)

load_dotenv()

root_agent = LlmAgent(
    name="commander",
    description="Agent that read, understand and execute commands pecfied in a README or external HTTPS URL document.",
    model=os.getenv("MODEL_NAME_AGENT", "gemini-2.5-flash"),
    instruction=ROOT_AGENT_INSTRUCTION,
    tools=[container_image_registry]
)

from google.adk.apps import App

app = App(root_agent=root_agent, name="command_execution_agent")
