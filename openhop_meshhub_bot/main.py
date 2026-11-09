"""openHop MeshHub Bot service entrypoint."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
from pathlib import Path

from .hub import BotHub

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("openhop.meshhub-bot")


def resolve_default_config_path() -> str:
    data_dir = os.environ.get("OPENHOP_PLUGIN_DATA")
    if data_dir:
        p = Path(data_dir).expanduser().resolve()
        p.mkdir(parents=True, exist_ok=True)
        return str(p / "config.json")
    return os.path.abspath("config.json")


async def async_main(config_path: str) -> None:
    hub = BotHub(config_path=config_path)
    loop = asyncio.get_running_loop()

    def _trigger_shutdown() -> None:
        logger.info("Received termination signal; initiating graceful shutdown...")
        hub.shutdown_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _trigger_shutdown)
        except NotImplementedError:
            signal.signal(sig, lambda *_: loop.call_soon_threadsafe(_trigger_shutdown))

    await hub.start()
    try:
        await hub.shutdown_event.wait()
    finally:
        try:
            await asyncio.wait_for(hub.stop(), timeout=3.8)
        except asyncio.TimeoutError:
            logger.warning("Shutdown timed out after 3.8s; exiting.")


def main() -> None:
    parser = argparse.ArgumentParser(description="openHop MeshHub Bot Service")
    parser.add_argument(
        "--config",
        type=str,
        default=resolve_default_config_path(),
        help="Path to data/config.json",
    )
    args = parser.parse_args()
    try:
        asyncio.run(async_main(args.config))
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
