import contextlib
import json
import os
import sys
import threading
from pathlib import Path
from typing import cast

import anyio
import discord
import psutil
from discord.ext import commands

from utils.bot import BOT_REPORTS_CHANNEL_ID, DOWNLOADS_FOLDER_PATH, RESTART_ARGS_MIN, VOLUME_SETTINGS_FILE_PATH, Bot
from utils.exception_reporter import ExceptionReporter

BOT_OWNER_ID = 191530044491956224


bot = Bot(owner_id=BOT_OWNER_ID)


@bot.event
async def on_application_command_error(ctx: discord.ApplicationContext, error: discord.DiscordException) -> None:
    """Handle errors from application commands."""
    if isinstance(error, commands.NotOwner):
        await ctx.respond("Sorry, only the bot owner can use this command!", ephemeral=True)
    elif isinstance(error, commands.NoPrivateMessage):
        await ctx.respond("Sorry, this command can't be used in a DM!")
    elif isinstance(error, commands.CheckAnyFailure):
        await ctx.respond("Sorry, you can't use this command!", ephemeral=True)
    else:
        if isinstance(error, (commands.CommandNotFound, commands.MissingPermissions, commands.BadArgument)):
            bot.logger.exception(error)
            raise error

        error = getattr(error, "original", error)

        if bot.exception_reporter:
            await bot.exception_reporter.report(
                error,
                context=(
                    f"**Command:** `/{ctx.command}`\n**User:** {ctx.author} (`{ctx.author.id}`)\n**Guild:** {ctx.guild}"
                ),
            )
        raise error


@bot.listen()
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState) -> None:
    """Handle changes in voice state to manage disconnects and channel tracking."""
    if after.channel:
        if member == bot.user:
            bot.per_guild_voice_channel_id[after.channel.guild.id] = after.channel.id
            return

    if not after.channel and member == bot.user:
        guild_id = cast("discord.VoiceChannel | discord.StageChannel", before.channel).guild.id
        bot.cleanup(guild_id)
        return

    if (
        before.channel
        and before.channel.id == bot.per_guild_voice_channel_id.get(before.channel.guild.id, None)
        and len([member for member in before.channel.members if not member.bot]) == 0
    ):
        bot.loop.create_task(bot.disconnect_countdown(before.channel))


@bot.listen(once=True)
async def on_ready() -> None:
    """Initialize the bot, load volume settings, and start background tasks."""
    try:
        if Path(VOLUME_SETTINGS_FILE_PATH).exists():
            async with await anyio.open_file(VOLUME_SETTINGS_FILE_PATH, "r") as file:
                bot.per_guild_volume_settings = json.loads(
                    await file.read(),
                    object_pairs_hook=lambda pairs: {int(k): v for k, v in pairs},
                )
        else:
            Path(VOLUME_SETTINGS_FILE_PATH).parent.mkdir(exist_ok=True, parents=True)
    except (OSError, json.JSONDecodeError):
        bot.logger.exception("Error upon reading %s", VOLUME_SETTINGS_FILE_PATH)

    if Path(DOWNLOADS_FOLDER_PATH).exists():
        for file in Path(DOWNLOADS_FOLDER_PATH).glob("*"):
            file.unlink(missing_ok=True)
            bot.logger.info("Deleted leftover file %s successfully.", file)
    else:
        Path(DOWNLOADS_FOLDER_PATH).mkdir(exist_ok=True, parents=True)

    bot.logger.info("Logged in as %s", bot.user)

    reports_channel = bot.get_channel(BOT_REPORTS_CHANNEL_ID)
    if reports_channel is None:
        with contextlib.suppress(discord.errors.DiscordException):
            reports_channel = await bot.fetch_channel(BOT_REPORTS_CHANNEL_ID)
    if reports_channel is not None:
        bot.memory_reporter.start(reports_channel, psutil.Process(os.getpid()))
        bot.exception_reporter = ExceptionReporter(bot, cast("discord.TextChannel", reports_channel))

    if bot.exception_reporter:
        bot.install_asyncio_handler(bot.exception_reporter)

    bot.watchdog_ticker.start()
    threading.Thread(target=bot.watchdog, daemon=True).start()

    await bot.wait_until_ready()
    await cast("discord.TextChannel", reports_channel).send(":arrows_counterclockwise: Finished restarting!")

    # Called after bot was restarted via command
    if len(sys.argv) >= RESTART_ARGS_MIN:
        channel = bot.get_channel(int(sys.argv[1]))
        msg = await cast("discord.TextChannel", channel).fetch_message(int(sys.argv[2]))
        await msg.edit(content="Restart has finished, I'm back!")


cogs_list = [
    "general",
    "songs",
]

for cog in cogs_list:
    bot.load_extension(f"cogs.{cog}")

if __name__ == "__main__":
    try:
        token = os.environ.get("DISCORD_TOKEN")
        if not token:
            bot.logger.error("DISCORD_TOKEN environment variable is not set. Exiting.")
            sys.exit(1)
        bot.run(token)
    except Exception:
        bot.logger.exception("Fatal error in outer run loop!")
        sys.exit(1)

# TODO: investigate why archive_id is empty ([2026-01-02 21:31:54,341]) -> fallback auf id?
# wenn der bot stuck ist und der watchdog ihn killt, schauen, ob man n feedback senden kann. entweder neue nachricht im
# letzten channel oder sogar aktuelle nachricht bearbeiten, die auf "thinking" steht (auch für den utils bot)
# einbauen, dass man vorskippen kann
# spotify playlist: metadaten aus link auslesen, dann aus yt zusammensuchen
# manchmal update sich die restart nachricht nicht. mehr logging einbauen, um rauszufinden, warum
# manchmal leavt bot nicht, logging einbauen wenn leute leaven/joinen und countdown checken
