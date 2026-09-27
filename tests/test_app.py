import asyncio

from slackmimic.app import _run_source
from slackmimic.config import ChannelMap, Config, Secrets


class FlakyReader:
    """Websocket always fails; polling runs until cancelled."""

    def __init__(self, stop: asyncio.Event, attempts_before_stop: int):
        self._stop = stop
        self._limit = attempts_before_stop
        self.ws_attempts = 0
        self.rescans = 0

    async def initialize_cursors(self, backfill_days):
        pass

    async def poll_once(self, rescan_threads=False):
        self.rescans += bool(rescan_threads)
        return 0

    async def run_websocket(self, stop):
        self.ws_attempts += 1
        if self.ws_attempts >= self._limit:
            self._stop.set()
        raise ConnectionError("socket refused")

    async def run_polling(self, stop):
        await asyncio.sleep(3600)


async def test_websocket_is_retried_after_polling_fallback():
    cfg = Config(
        secrets=Secrets("x", "d", "b"),
        channels=[ChannelMap("C_SRC", "C_DST", "general")],
        websocket_retry_seconds=0.01,
    )
    stop = asyncio.Event()
    reader = FlakyReader(stop, attempts_before_stop=3)
    await asyncio.wait_for(_run_source(reader, cfg, stop, 0), timeout=2)
    # Fell back to polling, then went back to the websocket — not polling forever.
    assert reader.ws_attempts == 3
    # Startup catch-up plus one after each retry window rescans threads.
    assert reader.rescans == 3
