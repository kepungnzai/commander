# # app/agent.py

# """ Command Execution Agent: read understand and execute command provided in a locally clone README and external HTTPS URL."""

# import os
# from dotenv import load_dotenv
# from google.adk.agents import LlmAgent
# from google.adk.models.lite_llm import LiteLlm

# # Create a LiteLLM model pointing to your local server
# model = LiteLlm(
#     model="openai/empero-ai/Qwen3.8-2B-Distill-GGUF",
#     api_base="http://localhost:8888/v1",  # Your local server
#     api_key="sk-unsloth-4d0a1b198bd177a2a72ee1954585342a" 
# )

# from app.prompt import ROOT_AGENT_INSTRUCTION
# from app.tools.container_toolset import (
#     container_image_finder_tool, run_container_command_tool
# )

# load_dotenv()

# root_agent = LlmAgent(
#     name="commander",
#     description="Agent that read, understand and execute commands specified in a README or external HTTPS URL document.",
#     model=model,
#     instruction=ROOT_AGENT_INSTRUCTION,
#     tools=[container_image_finder_tool, run_container_command_tool]
# )

# from google.adk.apps import App
# # runner
# app = App(root_agent=root_agent, name="app")


# from google.adk.agents.llm_agent import LlmAgent
# from google.adk.models.lite_llm import LiteLlm

# # Create a LiteLLM model pointing to your local server
# model = LiteLlm(
#     model="openai/empero-ai/Qwen3.8-2B-Distill-GGUF",
#     api_base="http://localhost:8888/v1",  # Your local server
#     api_key="sk-unsloth-4d0a1b198bd177a2a72ee1954585342a"  # Local servers don't require auth
# )

# # Create your agent
# # root_agent = LlmAgent(
# #     model=model,
# #     name="my_local_agent",
# #     description="An agent using a local LLM",
# #     instruction="You are a helpful assistant with access to tools.",
# #     tools=[],  # Add your tools here if needed
# # )

# # Define tools as regular Python functions (NO DECORATOR NEEDED)
# def calculator(expression: str) -> str:
#     """Evaluates a math expression.
    
#     Args:
#         expression (str): The math expression to evaluate.
#     """
#     return str(eval(expression))

# def get_weather(city: str, unit: str) -> str:
#     """Retrieves the weather for a city.
    
#     Args:
#         city (str): The city name.
#         unit (str): The temperature unit ('Celsius' or 'Fahrenheit').
#     """
#     # Your implementation here
#     return f"Weather for {city} in {unit} always 100 degrees."

# root_agent = LlmAgent(
#     model=model,
#     name="app",
#     description="An agent that can calculate and get weather information.",
#     instruction="You are a helpful assistant.",
#     tools=[calculator, get_weather],
# )





from openai import OpenAI

client = OpenAI(
    base_url="http://127.0.0.1:8888/v1",
    api_key="sk-unsloth-4d0a1b198bd177a2a72ee1954585342a",
)

response = client.chat.completions.create(
    model="empero-ai/Qwen3.8-2B-Distill-GGUF:Q4_K_M",
    messages=[{"role": "user", "content": "What is Unsloth?"}],
    stream=True,
)
for chunk in response:
    print(chunk.choices[0].delta.content or "", end="")