import litellm

resp = litellm.completion(
    model="openai/empero-ai/Qwen3.8-2B-Distill-GGUF",
    api_base="http://localhost:8888/v1",
    api_key="sk-unsloth-4d0a1b198bd177a2a72ee1954585342a",
    messages=[{"role": "user", "content": "What is the weather in Paris? Use the tool."}],
    tools=[{
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Get weather for a city",
            "parameters": {
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
            },
        },
    }],
)
msg = resp.choices[0].message
print("TOOL CALLS:", msg.tool_calls)
print("TEXT:", msg.content)