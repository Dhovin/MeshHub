import logging

logger = logging.getLogger("CommandDispatcher")
MAX_LORA_MSG_LEN = 135

def clamp_lora_message(text: str, max_len: int = MAX_LORA_MSG_LEN) -> str:
    """Clamps any message to at most max_len characters/bytes."""
    if len(text.encode("utf-8")) <= max_len:
        return text
    # Safely truncate
    truncated = text[:max_len - 3] + "..."
    while len(truncated.encode("utf-8")) > max_len and len(truncated) > 3:
        truncated = truncated[:-4] + "..."
    return truncated

class CommandDispatcher:
    def __init__(self, bot):
        self.bot = bot

    def build_help_message(self) -> str:
        """Constructs a single concise help message strictly <= 135 characters."""
        features = []
        if hasattr(self.bot, "module_manager") and self.bot.module_manager:
            mm = self.bot.module_manager
            is_mod_enabled = getattr(mm, "is_enabled", lambda m: m in getattr(mm, "modules", {}))
            if is_mod_enabled("net_bot"):
                features.append("#checkin (Net)")
            if is_mod_enabled("meshwars_bot"):
                features.append("!checkin (MeshWars)")
            if is_mod_enabled("autoresponce"):
                features.append("Auto-reply active")
            if is_mod_enabled("weather_bot"):
                features.append("NWS alerts active")

        if not features:
            features = ["!help (cmds)", "Bot active"]

        body = ", ".join(features)
        msg = f"MeshHub Bot: {body}. All replies <=135c."
        return clamp_lora_message(msg, MAX_LORA_MSG_LEN)

    async def handle_message(self, sender: str, text: str, channel, packet_meta: dict = None) -> bool:
        """
        Inspects message for recognized bot commands (e.g. !help).
        Returns True if handled, False otherwise.
        """
        if not text:
            return False

        clean = text.strip()
        cmd = clean.split()[0].lower() if clean else ""

        if cmd == "!help":
            # Airtime protection: per-user cooldown check
            if hasattr(self.bot, "can_send_user") and not self.bot.can_send_user(sender):
                logger.info(f"Rate-limited !help query from '{sender}'.")
                return True

            if hasattr(self.bot, "record_user_send"):
                self.bot.record_user_send(sender)

            help_msg = self.build_help_message()
            logger.info(f"Sending !help reply to '{sender}' (len={len(help_msg)}): {help_msg}")
            await self.bot.send_reply(sender=sender, channel=channel, text=help_msg)
            return True

        return False
