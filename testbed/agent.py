from google.adk.agents import Agent
from google.adk.models.lite_llm import LiteLlm

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

import os
from dotenv import load_dotenv
from google.adk.agents import LlmAgent
from google.adk.models.lite_llm import LiteLlm
from google.adk.tools import google_search

# Create a LiteLLM model pointing to your local server
model = LiteLlm(
    model="openai/empero-ai/Qwen3.8-2B-Distill-GGUF",
    api_base="http://localhost:8888/v1",  # Your local server
    api_key="sk-unsloth-4d0a1b198bd177a2a72ee1954585342a",
)

root_agent = Agent(
    name="tool_agent",
    model=model,
    instruction=(
            "You are a helpful assistant. "
            "Use get_weather for weather questions and add_numbers for addition. "
            "Never guess these values yourself; always call the tool."
            ),
    tools=[get_weather, add_numbers],
)

