import asyncio
import pytest
from app.collectors.prompt_reader import read_until_prompt, PromptTimeout

class FakeStream:
    def __init__(self, chunks): self.chunks=list(chunks)
    async def read(self, _):
        await asyncio.sleep(0)
        return self.chunks.pop(0) if self.chunks else ''

@pytest.mark.asyncio
async def test_prompt_across_chunks():
    out=await read_until_prompt(FakeStream(['hello\nAI','M>']), 'AIM>', 0.5)
    assert out.endswith('AIM>')

class SlowStream:
    async def read(self, _):
        await asyncio.sleep(1)
        return 'never'

@pytest.mark.asyncio
async def test_prompt_timeout():
    with pytest.raises(PromptTimeout):
        await read_until_prompt(SlowStream(), 'AIM>', 0.01)
