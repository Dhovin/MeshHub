import logging
import asyncio

logger = logging.getLogger("TemplateModule")

class Template:
    def __init__(self):
        self.name = "template"
        self.api = None
        self.config = {}
        
        # Schema for validation of configuration in config.json under modules.template
        self.config_schema = {
            "type": "object",
            "properties": {
                "enabled": {"type": "boolean"},
                "messagePrefix": {"type": "string"},
                "logChannel": {"type": "integer", "minimum": 0}
            },
            "required": ["enabled", "messagePrefix"]
        }
        self.unschedule = None
        self.unsubscribe_msg = None
        self.unsubscribe_conn = None

    def init(self, api, config):
        """
        Lifecycle hook called upon module loading.
        Injects the ModuleAPI instance and module-specific configuration.
        """
        self.api = api
        self.config = config
        logger.info(f"[{self.name}] Initialized with config: {config}")

    def start(self):
        """
        Lifecycle hook called when the bot has successfully started up.
        Use this to register event subscriptions and scheduled tasks.
        """
        logger.info(f"[{self.name}] Starting template module...")
        
        # 1. Subscribe to event bus messages (sync or async callbacks supported)
        self.unsubscribe_msg = self.api.subscribe("message", self._on_message)
        self.unsubscribe_conn = self.api.subscribe("connect", self._on_connect)
        
        # 2. Schedule a periodic task (e.g. every minute)
        # Cron syntax: minute hour day-of-month month day-of-week
        self.unschedule = self.api.schedule_task("* * * * *", self._periodic_log)
        
        logger.info(f"[{self.name}] Subscriptions and task scheduled.")

    def stop(self):
        """
        Lifecycle hook called during graceful shutdown.
        Enforces cleaning up subscriptions and scheduled tasks.
        """
        logger.info(f"[{self.name}] Stopping template module...")
        
        # Clean up subscriptions and tasks
        if self.unsubscribe_msg:
            self.unsubscribe_msg()
        if self.unsubscribe_conn:
            self.unsubscribe_conn()
        if self.unschedule:
            self.unschedule()
            
        logger.info(f"[{self.name}] Graceful stop complete.")

    def _on_message(self, data):
        """
        Triggered when a message event is published.
        """
        sender = data.get("sender", "unknown")
        text = data.get("text", "")
        channel = data.get("channel")
        if channel is None:
            logger.info(f"[{self.name}] Received direct message from {sender}: {text}")
        else:
            logger.info(f"[{self.name}] Received message from {sender} on channel {channel}: {text}")
        
        # Example of responding using api.send if prefix matches or ping
        if text.strip().lower() == "ping":
            prefix = self.config.get("messagePrefix", "[MeshHub]")
            reply = f"{prefix} pong"
            logger.info(f"[{self.name}] Ping received. Replying with: {reply}")
            
            # Send message back in the background since event handlers must not block
            asyncio.create_task(self._send_reply(sender, reply))

    async def _send_reply(self, recipient, text):
        # Format command to send message
        cmd = f"msg {recipient} {text}"
        res = await self.api.send(cmd)
        logger.info(f"[{self.name}] Sent message, response: {res}")

    def _on_connect(self, data):
        logger.info(f"[{self.name}] Connected to node successfully. Device payload: {data}")

    async def _periodic_log(self):
        """
        Periodic task run every minute.
        """
        state = self.api.get_state()
        battery = state.get("battery")
        neighbors = state.get("neighborCount", 0)
        logger.info(f"[{self.name}] Minute tick. Battery: {battery}%, Neighbors: {neighbors}")
        
        # Example of running device info query natively
        res = await self.api.send("infos")
        logger.info(f"[{self.name}] Natively fetched device info: {res}")
