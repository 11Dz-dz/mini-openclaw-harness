from collections.abc import AsyncIterator
import json

import httpx

from miniclaw.core.errors import (
    AuthenticationError,
    ModelProtocolError,
    ModelUnavailableError,
    RateLimitError,
)
from miniclaw.core.messages import ModelEvent, ModelRequest, ResponseCompleted, TextDelta
from miniclaw.model.encoding import encode_request
from miniclaw.model.openai_stream import decode_openai_chunk
from miniclaw.model.sse import SSEDecoder


class OpenAICompatibleClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=f"{base_url.rstrip('/')}/",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout,
            transport=transport,
        )

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        try:
            async with self._client.stream(
                "POST",
                "chat/completions",
                json=encode_request(request, stream=True),
            ) as response:
                self._raise_for_status(response)
                decoder = SSEDecoder()
                completed = False
                async for chunk in response.aiter_bytes():
                    for data in decoder.feed(chunk):
                        if data == "[DONE]":
                            if not completed:
                                raise ModelProtocolError(
                                    "model stream ended without completion"
                                )
                            return
                        try:
                            payload = json.loads(data)
                        except json.JSONDecodeError as exc:
                            raise ModelProtocolError(
                                "invalid streaming json"
                            ) from exc
                        if not isinstance(payload, dict):
                            raise ModelProtocolError("invalid streaming json")
                        for event in decode_openai_chunk(payload):
                            if isinstance(event, ResponseCompleted):
                                completed = True
                            yield event
                if not completed:
                    raise ModelProtocolError("model stream ended without completion")
        except httpx.TimeoutException as exc:
            raise ModelUnavailableError("model request timed out") from exc

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        if response.status_code in {401, 403}:
            raise AuthenticationError("model authentication failed")
        if response.status_code == 429:
            raise RateLimitError("model rate limit exceeded")
        if response.status_code >= 500:
            raise ModelUnavailableError("model service unavailable")
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise ModelProtocolError("model request failed") from exc

    async def aclose(self) -> None:
        await self._client.aclose()
