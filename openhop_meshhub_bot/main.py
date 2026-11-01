import os
import sys
import shutil
import asyncio
import logging
from openhop_meshhub_bot.core.bot import MeshHub

logger = logging.getLogger("openhop.meshhub-bot")

def main():
    plugin_data = os.environ.get("OPENHOP_PLUGIN_DATA")
    base_dir = os.path.dirname(os.path.abspath(__file__))
    assets_dir = os.path.join(base_dir, "share", "openhop", "plugins", "openhop.meshhub-bot")

    if plugin_data:
        os.makedirs(plugin_data, exist_ok=True)
        config_path = os.path.join(plugin_data, "config.json")
        default_config = os.path.join(assets_dir, "config.default.json")
        if not os.path.exists(config_path) and os.path.exists(default_config):
            shutil.copyfile(default_config, config_path)
            logger.info(f"Initialized default configuration at {config_path}")
        schema_path = os.path.join(base_dir, "..", "config", "schema.json")
        if not os.path.exists(schema_path):
            schema_path = os.path.join(assets_dir, "schema.json")
    else:
        config_path = "config/config.json"
        schema_path = "config/schema.json"

    modules_dir = os.path.join(base_dir, "modules")

    hub = MeshHub(config_path=config_path, schema_path=schema_path, modules_dir=modules_dir)
    hub.setup_logging()

    try:
        asyncio.run(hub.main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Service halted cleanly.")

if __name__ == "__main__":
    main()
