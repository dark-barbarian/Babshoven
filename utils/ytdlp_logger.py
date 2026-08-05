from utils.bot import DOWNLOAD_MESSAGE_INTERVAL, Bot


class YTDLPLogger:
    """Logger wrapper for yt_dlp."""

    def __init__(self, bot: Bot, guild_id: int) -> None:
        self.bot = bot
        self.guild_id = guild_id
        self.download_message_interval = DOWNLOAD_MESSAGE_INTERVAL

    def debug(self, msg: str) -> None:
        """Log debug messages and track recorded videos and ETA updates."""
        if "has already been recorded in" in msg:
            self.bot.guild_download_ids.setdefault(self.guild_id, []).append(
                msg.split(":")[0].removeprefix("[download] ")[len("\x1b[0;32m") : -len("\x1b[0m")]
            )
        if "ETA" in msg:
            if self.download_message_interval == DOWNLOAD_MESSAGE_INTERVAL:
                self.bot.logger.info(msg.strip())
            elif self.download_message_interval == 0:
                self.download_message_interval = DOWNLOAD_MESSAGE_INTERVAL + 1
            self.download_message_interval -= 1
        else:
            self.bot.logger.info(msg.strip())

    def info(self, msg: str) -> None:
        """Log info messages from yt_dlp."""
        self.bot.logger.info(msg.strip())

    def warning(self, msg: str) -> None:
        """Log warning messages from yt_dlp."""
        self.bot.logger.warning(msg.strip())

    async def error(self, msg: str) -> None:
        """Log error messages from yt_dlp."""
        self.bot.logger.error(msg.strip())

        if self.bot.exception_reporter:
            await self.bot.exception_reporter.report(RuntimeError(msg.strip()))

    def critical(self, msg: str) -> None:
        """Log critical messages from yt_dlp."""
        self.bot.logger.critical(msg.strip())
