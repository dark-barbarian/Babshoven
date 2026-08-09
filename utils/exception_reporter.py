from __future__ import annotations

import time
import traceback
from pathlib import Path
from typing import TYPE_CHECKING

import discord

if TYPE_CHECKING:
    from utils.bot import Bot


class ExceptionReporter:
    """Class to report exceptions to a designated Discord channel."""

    def __init__(self, bot: Bot, channel: discord.TextChannel) -> None:
        self.bot = bot
        self.channel = channel

    async def report(self, exception: BaseException, *, context: str | None = None) -> None:
        """Report an exception to the designated Discord channel with detailed information."""
        tb = exception.__traceback__
        if tb is None:
            self.bot.logger.error("No traceback available for exception: %r", exception)
            return

        while tb.tb_next:
            tb = tb.tb_next

        filename = Path(tb.tb_frame.f_code.co_filename).name
        function = tb.tb_frame.f_code.co_name
        line = tb.tb_lineno

        embed = self.bot.create_embed(title="🚨 Exception occurred", description=context, color=discord.Colour.red())
        embed.add_field(name="Type", value=f"`{type(exception).__name__}`", inline=True)
        embed.add_field(name="Location", value=f"`{filename}:{line}`", inline=True)
        embed.add_field(name="Function", value=f"`{function}()`", inline=True)
        embed.add_field(name="Time", value=f"<t:{int(time.time())}:F>", inline=False)

        await self.channel.send(embed=embed)

        traceback_text = "".join(
            traceback.format_exception(
                type(exception),
                exception,
                exception.__traceback__,
            )
        )

        while traceback_text:
            chunk = traceback_text[:1988]
            traceback_text = traceback_text[1988:]

            await self.channel.send(f"```py\n{chunk}\n```")
