"""Bounded probes of this installation's configured public A2A Card URLs."""
import asyncio
import time
import weakref
from urllib.parse import quote, urlsplit

import httpx

from ..config import settings
from ..security.pinned_http import SSRFGuardAsyncTransport

_cache: dict[tuple[str, str], tuple[float, str]] = {}
_locks = weakref.WeakKeyDictionary()


def invalidate_team(slug: str):
    for key in list(_cache):
        if key[1] == slug:
            _cache.pop(key, None)


async def team_states(teams):
    loop = asyncio.get_running_loop()
    lock = _locks.setdefault(loop, asyncio.Lock())
    # Concurrent page requests share one refresh, not separate probe batches.
    async with lock:
        return await _team_states(teams)


async def _team_states(teams):
    base = settings.public_url.rstrip("/")
    host = urlsplit(base).hostname
    transport = SSRFGuardAsyncTransport(allowed_hosts=frozenset({host}) if host else frozenset())
    semaphore = asyncio.Semaphore(10)
    async with httpx.AsyncClient(transport=transport, timeout=3, follow_redirects=False) as client:
        async def probe(team):
            key = (base, team["slug"])
            cached = _cache.get(key)
            if cached and cached[0] > time.monotonic():
                return team["master_id"], cached[1]
            async with semaphore:
                async def check_card():
                    async with client.stream("GET", f"{base}/a2a/{quote(team['slug'], safe='')}/.well-known/agent-card.json") as response:
                        return "live" if response.is_success else "pending"
                try:
                    state = await asyncio.wait_for(check_card(), timeout=3)
                except (httpx.HTTPError, ValueError, TimeoutError):
                    state = "pending"
                _cache[key] = (time.monotonic() + 30, state)
                return team["master_id"], state
        rows = await asyncio.gather(*(probe(team) for team in teams))
    active = {(base, team["slug"]) for team in teams}
    for key in list(_cache):
        if key not in active:
            _cache.pop(key, None)
    return dict(rows)
