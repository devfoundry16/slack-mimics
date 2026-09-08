"""Entry point: wire the units together and run the mirror daemon."""

from __future__ import annotations

import argparse
import asyncio
import logging
from typing import Optional

from .client_hs import AuthError, HeartStampClient
from .config import Config, load_config
from .models import SourceEvent
from .sink.client_target import TargetClient
from .sink.poster import Poster
from .source.reader import SourceReader
from .state.store import StateStore
from .transform.transformer import Transformer, is_actionable
from .users import UserResolver

log = logging.getLogger("slackmimic")


async def _consume(
    queue: "asyncio.Queue[SourceEvent]",
    transformer: Transformer,
    poster: Poster,
    stop: asyncio.Event,
) -> None:
    """Drain the queue: transform each event and apply it to the target."""
    while not stop.is_set():
        event = await queue.get()
        try:
            if not is_actionable(event):
                continue
            action = await transformer.transform(event)
            if action is None:
                continue
            await poster.apply(action)
        except AuthError:
            raise
        except Exception as exc:
            log.warning("failed to mirror %s@%s: %s", event.kind, event.ts, exc)
        finally:
            queue.task_done()


async def _run_source(
    reader: SourceReader, config: Config, stop: asyncio.Event, backfill_days: float
) -> None:
    """Prefer the websocket transport; fall back to polling on failure."""
    await reader.initialize_cursors(backfill_days)
    if backfill_days > 0:
        log.info("backfilling up to %g day(s) of history…", backfill_days)
    # Catch up on anything missed since the last run.
    try:
        await reader.poll_once()
    except AuthError:
        raise
    except Exception as exc:
        log.warning("initial catch-up poll failed: %s", exc)

    if config.use_websocket:
        while not stop.is_set():
            try:
                await reader.run_websocket(stop)
                # Clean return means the socket closed; reconnect after catch-up.
                await reader.poll_once()
            except AuthError:
                raise
            except Exception as exc:
                log.warning("websocket unavailable (%s); falling back to polling", exc)
                await reader.run_polling(stop)
                return
    else:
        await reader.run_polling(stop)


async def run(config: Config, backfill_days: Optional[float] = None) -> None:
    if backfill_days is None:
        backfill_days = config.backfill_days
    queue: "asyncio.Queue[SourceEvent]" = asyncio.Queue()
    stop = asyncio.Event()

    async with (
        StateStore(config.db_path) as store,
        HeartStampClient(config.secrets.hs_xoxc_token, config.secrets.hs_d_cookie) as hs,
        TargetClient(config.secrets.target_bot_token) as target,
    ):
        resolver = UserResolver(hs, store)
        transformer = Transformer(config, resolver)
        poster = Poster(target, store, hs)
        reader = SourceReader(hs, config, store, queue)

        log.info("mirroring %d channel(s)", len(config.channels))
        consumer = asyncio.create_task(_consume(queue, transformer, poster, stop))
        source = asyncio.create_task(_run_source(reader, config, stop, backfill_days))

        done, pending = await asyncio.wait(
            {consumer, source}, return_when=asyncio.FIRST_EXCEPTION
        )
        stop.set()
        for task in pending:
            task.cancel()
        for task in done:
            exc = task.exception()
            if exc:
                raise exc


def main() -> None:
    parser = argparse.ArgumentParser(description="Mirror Slack messages into your own workspace.")
    parser.add_argument("-c", "--config", default="config.yaml", help="path to config.yaml")
    parser.add_argument("--env", default=None, help="path to .env file")
    parser.add_argument(
        "--backfill",
        type=float,
        default=None,
        metavar="DAYS",
        help="on first run, mirror this many days of history (overrides config)",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    config = load_config(args.config, env_file=args.env)
    try:
        asyncio.run(run(config, backfill_days=args.backfill))
    except AuthError as exc:
        log.error("authentication failed: %s", exc)
        raise SystemExit(2)
    except KeyboardInterrupt:
        log.info("stopped")


if __name__ == "__main__":
    main()
