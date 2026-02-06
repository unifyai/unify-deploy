"""UniLLM OpenAI-compatible chat completions endpoint."""
import json
import logging
from typing import AsyncGenerator

import httpx
import unillm
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from communication.helpers import ORCHESTRA_URL
from communication.unillm.schema import ChatCompletionRequest

router = APIRouter()
logger = logging.getLogger(__name__)


async def _authenticate_api_key(api_key: str) -> dict:
    """
    Validate the caller's API key by calling Orchestra's /user/basic-info endpoint.

    Returns the user info dict on success, raises HTTPException on failure.
    """
    async with httpx.AsyncClient() as client:
        response = await client.get(
            f"{ORCHESTRA_URL}/user/basic-info",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=10.0,
        )

    if response.status_code != 200:
        logger.warning(f"API key authentication failed: {response.status_code}")
        raise HTTPException(
            status_code=401,
            detail="Invalid API key.",
        )

    return response.json()


@router.post("/chat/completions")
async def chat_completions(
    request_body: ChatCompletionRequest,
    request: Request,
):
    """
    OpenAI-compatible chat completions endpoint via UniLLM.

    Routes requests through UniLLM for caching, cost tracking, and multi-provider
    support. The caller's API key is extracted from the Authorization header and
    validated against Orchestra before forwarding to UniLLM.

    The model should be specified in UniLLM format: "model@provider"
    (e.g., "claude-sonnet-4-20250514@anthropic", "gpt-4o@openai").
    """
    # Extract API key from the Authorization Bearer header
    auth_header = request.headers.get("authorization", "")
    api_key = auth_header.replace("Bearer ", "") if auth_header.startswith("Bearer ") else ""

    if not api_key:
        raise HTTPException(status_code=401, detail="Missing API key.")

    # Validate user exists via Orchestra
    await _authenticate_api_key(api_key)

    # Convert messages to dict format for unillm
    messages = [msg.model_dump(exclude_none=True) for msg in request_body.messages]

    if request_body.stream:
        return await _stream_response(request_body, messages, api_key)
    else:
        return await _non_stream_response(request_body, messages, api_key)


async def _non_stream_response(
    request_body: ChatCompletionRequest,
    messages: list,
    api_key: str,
) -> dict:
    """Handle non-streaming chat completion."""
    client = unillm.AsyncUnify(
        request_body.model,
        api_key=api_key,
        temperature=request_body.temperature,
        max_completion_tokens=(
            request_body.max_completion_tokens or request_body.max_tokens
        ),
        top_p=request_body.top_p,
        frequency_penalty=request_body.frequency_penalty,
        presence_penalty=request_body.presence_penalty,
        stop=request_body.stop,
        seed=request_body.seed,
        tools=request_body.tools,
        tool_choice=request_body.tool_choice,
        response_format=request_body.response_format,
        return_full_completion=True,
    )

    response = await client.generate(messages=messages)
    return response.model_dump()


async def _stream_response(
    request_body: ChatCompletionRequest,
    messages: list,
    api_key: str,
) -> StreamingResponse:
    """Handle streaming chat completion with SSE."""

    async def generate() -> AsyncGenerator[str, None]:
        client = unillm.AsyncUnify(
            request_body.model,
            api_key=api_key,
            stream=True,
            stream_options={"include_usage": True},
            temperature=request_body.temperature,
            max_completion_tokens=(
                request_body.max_completion_tokens or request_body.max_tokens
            ),
            top_p=request_body.top_p,
            frequency_penalty=request_body.frequency_penalty,
            presence_penalty=request_body.presence_penalty,
            stop=request_body.stop,
            seed=request_body.seed,
            tools=request_body.tools,
            tool_choice=request_body.tool_choice,
            response_format=request_body.response_format,
            return_full_completion=True,
        )

        async for chunk in client.generate(messages=messages):
            chunk_data = chunk.model_dump() if hasattr(chunk, "model_dump") else chunk
            yield f"data: {json.dumps(chunk_data)}\n\n"

        yield "data: [DONE]\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # Disable nginx buffering
        },
    )
