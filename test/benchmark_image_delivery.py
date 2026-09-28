"""Synthetic loopback image reads, not GPU or end-to-end production latency."""
import asyncio
import importlib.util
import json
from pathlib import Path
from statistics import median
from time import perf_counter

import aiohttp
from aiohttp import web

spec = importlib.util.spec_from_file_location('prefetch', Path(__file__).resolve().parents[1] /
    'backend/open_webui/utils/images/prefetch.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


async def main():
    async def image(request):
        await asyncio.sleep(0.05)
        return web.Response(body=request.match_info['index'].encode() * 65536, content_type='image/png')
    app = web.Application()
    app.router.add_get('/{index}', image)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 0)
    await site.start()
    base = f'http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}'
    timings = {'serial_ms': [], 'prefetch_ms': []}
    try:
        async with aiohttp.ClientSession() as session:
            async def load(i):
                async with session.get(f'{base}/{i}') as response:
                    response.raise_for_status()
                    return await response.read()
            for _ in range(5):
                for key in timings:
                    start = perf_counter()
                    if key == 'serial_ms':
                        values = [await load(i) for i in range(6)]
                    else:
                        async with module.prefetch_images(range(6), load) as results:
                            values = [value async for value in results]
                    timings[key].append(round((perf_counter() - start) * 1000, 3))
                    assert values == [str(i).encode() * 65536 for i in range(6)]
    finally:
        await runner.cleanup()
    print(json.dumps({'scope': '6 synthetic 64KiB loopback responses, 50ms delay each; no GPU',
                      'runs': timings, 'median_ms': {key: median(values) for key, values in timings.items()}}, indent=2))


if __name__ == '__main__':
    asyncio.run(main())
