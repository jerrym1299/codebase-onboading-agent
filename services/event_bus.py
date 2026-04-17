import asyncio
from collections import defaultdict

_subscribers: dict[str, list[asyncio.Queue]] = defaultdict(list)


def subscribe(session_id: str) -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue()
    _subscribers[session_id].append(q)
    return q


def unsubscribe(session_id: str, q: asyncio.Queue):
    try:
        _subscribers[session_id].remove(q)
    except ValueError:
        pass
    if not _subscribers[session_id]:
        del _subscribers[session_id]


async def publish(session_id: str, event: dict):
    for q in _subscribers.get(session_id, []):
        await q.put(event)
