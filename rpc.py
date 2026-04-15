import os
import httpx
from typing import Any


class ZebradRPC:
    def __init__(self, url: str | None = None):
        self.url = url or os.environ.get('ZEBRAD_RPC_URL', 'http://127.0.0.1:8232')
        self.client = httpx.AsyncClient(timeout=10.0)

    async def call(self, method: str, params: list | None = None) -> Any:
        payload = {
            'jsonrpc': '2.0',
            'method': method,
            'params': params or [],
            'id': 1,
        }
        r = await self.client.post(self.url, json=payload)
        r.raise_for_status()
        data = r.json()
        err = data.get('error')
        if err:
            raise RuntimeError('rpc error for ' + method + ': ' + str(err))
        return data.get('result')

    async def close(self) -> None:
        await self.client.aclose()
