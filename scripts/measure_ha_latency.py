"""Read-only Home Assistant latency check. No OpenAI session or device actions."""
import argparse
import asyncio
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from r2.config import DATA
from r2.ha_transport import prepare_address
from r2.server import make_ha


async def main(args):
    ha = make_ha()
    if ha is None:
        raise SystemExit('Home Assistant is not configured.')
    ha.keep_alive = True
    started = time.perf_counter()
    await prepare_address(ha.url)
    report = {'dns_preparation_ms': round((time.perf_counter() - started) * 1000, 2), 'reads': []}
    try:
        for attempt in range(args.runs):
            started = time.perf_counter()
            result = await ha.get_lights('all')
            row = {'run': attempt + 1, 'status_read_ms': round((time.perf_counter() - started) * 1000, 2),
                   'light_count': len(result['lights'])}
            report['reads'].append(row)
            print(json.dumps(row), flush=True)
        connection = ha.connection
        report['metadata_events'] = dict(connection.subscriptions)
        async with connection.watch_states([]) as watch:
            report['state_events_enabled'] = watch.enabled
    finally:
        await ha.aclose()
    report['closed'] = not connection.connected and connection.ws.close_code is not None
    DATA.mkdir(exist_ok=True)
    (DATA / 'ha-latency-latest.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runs', type=int, choices=range(1, 11), default=3)
    asyncio.run(main(parser.parse_args()))
