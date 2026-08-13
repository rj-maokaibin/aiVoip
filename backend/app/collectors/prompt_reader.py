import asyncio

class PromptTimeout(RuntimeError):
    pass

class PromptSessionClosed(RuntimeError):
    pass

async def read_until_prompt(stream, marker: str, timeout: float, chunk_size: int = 1024) -> str:
    chunks: list[str] = []
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise PromptTimeout(marker)
        try:
            chunk = await asyncio.wait_for(stream.read(chunk_size), timeout=remaining)
        except asyncio.TimeoutError as exc:
            raise PromptTimeout(marker) from exc
        if not chunk:
            raise PromptSessionClosed(marker)
        chunks.append(chunk)
        text = ''.join(chunks)
        if marker in text:
            return text
