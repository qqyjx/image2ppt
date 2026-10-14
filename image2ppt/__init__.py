"""image2ppt: convert slide images into editable PPTX files with native shapes."""
from .llm import AnthropicAPIClient, ClaudeCLIClient, LLMClient, ReplayClient
from .pipeline import convert
from .tidy import tidy

__all__ = ["AnthropicAPIClient", "ClaudeCLIClient", "LLMClient", "ReplayClient", "convert", "tidy"]
__version__ = "0.1.0"
