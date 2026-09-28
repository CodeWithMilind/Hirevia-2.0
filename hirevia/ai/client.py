"""NVIDIA/OpenAI-compatible AI client"""

import json
import logging
from typing import Optional

from openai import AsyncOpenAI

from hirevia.config import Config

logger = logging.getLogger(__name__)


class NVIDIAClient:
    def __init__(self, config: Config, api_key: str):
        self.config = config
        self.client = AsyncOpenAI(
            api_key=api_key,
            base_url=config.nvidia_base_url,
        )
        self.model = config.nvidia_model

    async def chat_completion(
        self,
        messages: list[dict],
        temperature: float = 0.1,
        max_tokens: int = 500,
        response_format: Optional[dict] = None,
    ) -> str:
        """Get chat completion from NVIDIA API."""
        try:
            kwargs = {
                "model": self.model,
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
            }
            if response_format:
                kwargs["response_format"] = response_format

            response = await self.client.chat.completions.create(**kwargs)
            return response.choices[0].message.content or ""
        except Exception as e:
            logger.error(f"NVIDIA API error: {e}")
            raise

    async def structured_completion(
        self,
        messages: list[dict],
        schema: dict,
        temperature: float = 0.1,
        max_tokens: int = 500,
        retries: int = 1,
    ) -> dict:
        """Get structured JSON completion with validation."""
        response_format = {"type": "json_schema", "json_schema": {"name": "response", "schema": schema}}

        last_error = None
        for attempt in range(retries + 1):
            try:
                content = await self.chat_completion(
                    messages=messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    response_format=response_format,
                )
                return json.loads(content)
            except json.JSONDecodeError as e:
                last_error = e
                logger.warning(f"Invalid JSON from AI (attempt {attempt + 1}): {e}")
                if attempt < retries:
                    # Add a correction message
                    messages.append({
                        "role": "assistant",
                        "content": content,
                    })
                    messages.append({
                        "role": "user",
                        "content": "The previous response was invalid JSON. Please respond with ONLY valid JSON matching the schema.",
                    })
            except Exception as e:
                last_error = e
                logger.error(f"AI completion error: {e}")
                break

        raise last_error or Exception("AI completion failed")