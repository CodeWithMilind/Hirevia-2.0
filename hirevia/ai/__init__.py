"""AI module for Hirevia"""

from hirevia.ai.client import NVIDIAClient, OpenAICompatibleClient
from hirevia.ai.matcher import AIMatcher
from hirevia.ai.providers import AIProvider, AIProviderRouter, build_ai_provider_router

__all__ = [
	"AIProvider",
	"AIProviderRouter",
	"AIMatcher",
	"NVIDIAClient",
	"OpenAICompatibleClient",
	"build_ai_provider_router",
]