"""OpenAI-compatible request/response schemas for UniLLM proxy."""
from typing import List, Literal, Optional, Union

from pydantic import BaseModel


class ContentPart(BaseModel):
    """Content part for multimodal messages (text or image)."""

    type: Literal["text", "image_url"]
    text: Optional[str] = None
    image_url: Optional[dict] = None  # {"url": "data:image/png;base64,..."}


class ChatMessage(BaseModel):
    """OpenAI-compatible chat message."""

    role: Literal["system", "user", "assistant", "tool"]
    content: Union[str, List[ContentPart], None] = None
    name: Optional[str] = None
    tool_calls: Optional[List[dict]] = None
    tool_call_id: Optional[str] = None


class ChatCompletionRequest(BaseModel):
    """OpenAI-compatible chat completion request."""

    model: str  # e.g., "claude-sonnet-4-20250514@anthropic"
    messages: List[ChatMessage]
    stream: bool = False
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    max_completion_tokens: Optional[int] = None
    top_p: Optional[float] = None
    frequency_penalty: Optional[float] = None
    presence_penalty: Optional[float] = None
    stop: Optional[Union[str, List[str]]] = None
    seed: Optional[int] = None
    tools: Optional[List[dict]] = None
    tool_choice: Optional[Union[str, dict]] = None
    response_format: Optional[dict] = None


class Usage(BaseModel):
    """Token usage statistics."""

    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


class ResponseMessage(BaseModel):
    """Assistant response message."""

    role: str = "assistant"
    content: Optional[str] = None
    tool_calls: Optional[List[dict]] = None


class Choice(BaseModel):
    """Response choice."""

    index: int
    message: ResponseMessage
    finish_reason: Optional[str] = None


class ChatCompletionResponse(BaseModel):
    """OpenAI-compatible chat completion response."""

    id: str
    object: Literal["chat.completion"] = "chat.completion"
    created: int
    model: str
    choices: List[Choice]
    usage: Usage
