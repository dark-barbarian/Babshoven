"""Refactor-preview copy of main.py with style and safety improvements.

This file is a cleaned, PEP8-friendly preview kept inside
`refactor_preview/` so you can review before applying changes to the
main project.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
import threading
import time as _time
from datetime import datetime, time, timedelta
from typing import NotRequired, TypedDict, Union, cast
from zoneinfo import ZoneInfo

import discord
import psutil
import yt_dlp
from discord import option
from discord.channel import VocalGuildChannel
from discord.ext import commands, tasks

from observable_set import ObservableSet

__all__ = ["bot"]


logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s]: %(message)s",
    handlers=[logging.FileHandler("babshoven.log"), logging.StreamHandler()],
)


class Song(TypedDict):
    archive_id: str
    id: str
    filename: str
    title: str
    song_link: str
    duration_string: str
    duration: int
    starting_time: NotRequired[datetime]
    passed_time: NotRequired[timedelta]
    passed_time_until_pause: NotRequired[timedelta]


class YTDLPLogger:
    """Logger adapter passed to yt_dlp to capture and relay logs.

    It records download ids and throttles ETA logs to avoid spam.
    """

    def __init__(self, guild_id: int) -> None:
        self.logger = logging.getLogger()
        self.guild_id = guild_id
        self.download_message_interval = 15

    def debug(self, msg: str) -> None:
        if "has already been recorded in" in msg:
            _guild_download_ids.setdefault(self.guild_id, []).append(
                msg.split(":")[0].removeprefix("[download] ")[
                    len("\x1b[0;32m") : -len("\x1b[0m")
                ]
            )
        if "ETA" in msg:
            if self.download_message_interval == 15:
                self.logger.info(msg.strip())
            elif self.download_message_interval == 0:
                self.download_message_interval = 16
            self.download_message_interval -= 1
        else:
            self.logger.info(msg.strip())

    def info(self, msg: str) -> None:
        self.logger.info(msg.strip())

    def warning(self, msg: str) -> None:
        self.logger.warning(msg.strip())

    def error(self, msg: str) -> None:
        self.logger.error(msg.strip())

    def critical(self, msg: str) -> None:
        self.logger.critical(msg.strip())


bot = commands.Bot(owner_id=191530044491956224)
watchdog_last_tick = _time.time()

##################################################################
############################ GENERAL #############################
##################################################################

DISCONNECTION_COUNTDOWN: int = 300  # seconds until disconnect while inactive
BOT_REPORTS_CHANNEL_ID = 1403711339355963443
MEMORY_INTERVAL_HOURS = 12  # must be 0 < h <= 24

_guild_voice_channel_ids: dict[int, int] = {}


def create_embed(
    title: str | None = None,
    description: str | None = None,
    color: int | None = None,
    footer: str | None = None,
) -> discord.Embed:
    """Create a simple embed with optional footer.

    Kept small as a helper for potential future command responses.
    """

    embed_var = discord.Embed(title=title, description=description, color=color)
    embed_var.set_footer(text=footer)
    return embed_var


def cleanup(guild_id: int) -> None:
    """Clear state for a guild: remove queued songs and loop settings."""

    guild_queue = _all_guild_song_queues.get(guild_id, []).copy()
    for song in guild_queue:
        try:
            _all_guild_song_queues.get(guild_id, []).remove(song)
        except ValueError:
            # defensive: the song was already removed
            pass
        remove_downloaded_song(song)

    _all_guild_song_queues.pop(guild_id, None)
    _all_guild_loop_settings.pop(guild_id, None)


def find_dict_by_id(to_search_in: list[Song], item_id: str) -> list[Song]:
    """Find dictionaries in a list matching `id` field."""

    filtered_list = [d for d in to_search_in if bool(d)]
    return [d for d in filtered_list if d["id"] == item_id]


async def disconnect_countdown(channel: VocalGuildChannel) -> None:
    countdown = DISCONNECTION_COUNTDOWN // 10
    while len(channel.members) == 1 and countdown > 0:
        countdown -= 1
        await asyncio.sleep(10)

    if countdown == 0:
        vcs = list(
            filter(
                lambda vc: channel.guild.id == cast(discord.Guild, vc.guild).id,
                cast(list[discord.VoiceClient], bot.voice_clients),
            )
        )
        if not vcs:
            logging.info("I tried to leave, but I already was disconnected earlier.")
            return

        logging.info("Left the voice channel after feeling lonely.")
        vc: discord.VoiceClient = vcs[0]
        cleanup(channel.guild.id)
        if vc.is_playing() or vc.is_paused():
            vc.stop()
        await vc.disconnect()


@tasks.loop(
    time=tuple(
        time(hour=(i * MEMORY_INTERVAL_HOURS) % 24, tzinfo=ZoneInfo("Europe/Berlin"))
        for i in range(24 // MEMORY_INTERVAL_HOURS)
    )
)
async def memory_reporter(
    channel: discord.TextChannel, process: psutil.Process
) -> None:
    mem_mb = process.memory_info().rss / 1024 / 1024
    total_mb = psutil.virtual_memory().total / 1024 / 1024
    cpu_percent = process.cpu_percent(interval=None)
    await channel.send(
        f"🖥 Memory: {mem_mb:.2f} MB / {total_mb:.0f} MB | CPU: {cpu_percent:.1f}%"
    )


@tasks.loop(seconds=5)
async def watchdog_ticker() -> None:
    global watchdog_last_tick
    watchdog_last_tick = _time.time()


def watchdog(interval: int = 5, timeout: int = 15) -> None:
    while True:
        _time.sleep(interval)
        if _time.time() - watchdog_last_tick > timeout:
            logging.error("Bot appears frozen, killing the process...")
            os._exit(1)


@bot.slash_command(name="ping", description="Check the bot's latency")
async def ping(ctx: discord.ApplicationContext) -> None:
    await ctx.respond(f"Latency: {round(bot.latency * 1000)} ms")


@bot.slash_command(name="restart", description="Restart the bot (owner only)")
@commands.is_owner()
async def restart(ctx: discord.ApplicationContext) -> None:
    interaction = await ctx.respond("Restarting...")
    response = await cast(discord.Interaction, interaction).original_response()
    os.execv(
        sys.executable,
        [sys.executable] + sys.argv + [str(response.channel.id), str(response.id)],
    )


@bot.slash_command(name="clear_cache", description="Clear download cache (owner only)")
@commands.is_owner()
async def clear_cache(ctx: discord.ApplicationContext) -> None:
    _download_archive.clear()
    await ctx.respond("Cleared download cache.")


@bot.slash_command(
    name="override_limits", description="Override the bot's limits (owner only)"
)
@option(
    "max_song_length",
    description="Maximum song length in minutes",
    required=False,
    input_type=int,
)
@option(
    "playlist_limit",
    description="Maximum number of songs in a playlist",
    required=False,
    input_type=int,
)
@commands.is_owner()
async def override_limits(
    ctx: discord.ApplicationContext, max_song_length: int, playlist_limit: int
) -> None:
    if not (max_song_length or playlist_limit):
        await ctx.respond("You need to specify at least one option.", ephemeral=True)
        return

    await ctx.defer()

    global SONG_MAX_LENGTH_MINUTES, PLAYLIST_SONGS_LIMIT
    if max_song_length:
        await ctx.respond(
            f"Changed maximum song duration from {SONG_MAX_LENGTH_MINUTES} to {max_song_length}!"
        )
        SONG_MAX_LENGTH_MINUTES = max_song_length
    if playlist_limit:
        await ctx.respond(
            f"Changed maximum number of songs per playlist from {PLAYLIST_SONGS_LIMIT} to {playlist_limit}!"
        )
        PLAYLIST_SONGS_LIMIT = playlist_limit

    for opt in cast(discord.SlashCommand, play).options:
        if opt.name == "playlist_limit":
            opt.description = (
                opt.description.rsplit(" ", 1)[0] + " " + str(PLAYLIST_SONGS_LIMIT)
            )
            opt.max_value = PLAYLIST_SONGS_LIMIT
            break

    await bot.sync_commands()


@bot.event
async def on_application_command_error(
    ctx: discord.ApplicationContext, error: discord.DiscordException
) -> None:
    if isinstance(error, commands.NotOwner):
        await ctx.respond(
            "Sorry, only the bot owner can use this command!", ephemeral=True
        )
    elif isinstance(error, commands.NoPrivateMessage):
        await ctx.respond("Sorry, this command can't be used in a DM!")
    else:
        logging.error(error)
        raise error


##################################################################
######################### MUSIC METHODS ##########################
##################################################################

DEFAULT_BOT_VOLUME = 0.2
VOLUME_SETTINGS_FILE_PATH = "./volumesettings.json"
SONG_MAX_LENGTH_MINUTES = 60
PLAYLIST_SONGS_LIMIT = 50

_all_guild_volume_settings: dict[int, float] = {}
_pause_after_play: dict[int, bool] = {}
_guild_download_ids: dict[int, list[str]] = {}
_download_archive: ObservableSet = ObservableSet(logger=logging.getLogger())
_is_downloading_per_guild: dict[int, bool] = {}
_all_guild_song_queues: dict[int, list[Song]] = {}
_guild_added_song: dict[int, Song] = {}
_all_guild_loop_settings: dict[int, int] = {}
_stop_downloading_interaction: discord.Interaction | discord.WebhookMessage | None = (
    None
)


def is_active(ctx: discord.ApplicationContext) -> bool:
    return bool(
        ctx.voice_client
        and (ctx.voice_client.is_playing() or ctx.voice_client.is_paused())
    )


def current_song_info(ctx: discord.ApplicationContext) -> str:
    try:
        current_song = _all_guild_song_queues[ctx.guild_id][0]
    except (KeyError, IndexError):
        return ""

    loops = _all_guild_loop_settings.get(ctx.guild_id, 0)

    if not current_song or not ctx.voice_client:
        return ""

    if ctx.voice_client.is_playing():
        now = datetime.now()
        runtime = str(now - current_song.get("starting_time", now)).split(".")[0]
    elif ctx.voice_client.is_paused():
        runtime = str(current_song.get("passed_time_until_pause", timedelta(0))).split(
            "."
        )[0]
        header = "# [Playback is paused]\n"
    else:
        runtime = "0:00:00"
        header = ""

    duration_string = current_song["duration_string"]
    if current_song["duration"] < 60:
        duration_string = "0:" + duration_string.zfill(2)
    if current_song["duration"] < 60 * 60:
        runtime = runtime.removeprefix("0:").removeprefix("0")

    title = current_song["title"]
    link = current_song["song_link"]
    response = f"{header}- **[{title}](<{link}>) - ({runtime} / {duration_string})"

    if loops != 0:
        loop_text = f" [Looped: {loops if loops > 0 else '\u221e'} time{'s' if loops != 1 else ''} left]"
        response += loop_text

    return response + "**"


def remove_downloaded_song(current_song: Song | None) -> None:
    if not current_song:
        return

    filename = current_song["filename"]
    all_songs = _all_guild_song_queues.values()
    if not any(song["filename"] == filename for queue in all_songs for song in queue):
        try:
            os.remove(filename)
            logging.info("Deleted %s successfully.", filename)
            _download_archive.discard(current_song["archive_id"])
        except FileNotFoundError:
            logging.error("Deleting %s failed, file was not found.", filename)


async def play_next(ctx: discord.ApplicationContext) -> None:
    guild_id = ctx.guild_id
    volume = _all_guild_volume_settings.get(guild_id, DEFAULT_BOT_VOLUME)
    loops = _all_guild_loop_settings.get(guild_id, 0)

    if loops == 0 and len(_all_guild_song_queues.get(guild_id, [])) == 0:
        return
    if loops != 0:
        _all_guild_loop_settings[guild_id] -= 1

    passed_time = _all_guild_song_queues[guild_id][0].get(
        "passed_time", timedelta(seconds=0)
    )
    _all_guild_song_queues[guild_id][0]["starting_time"] = datetime.now() - passed_time

    source = await discord.FFmpegOpusAudio.from_probe(
        _all_guild_song_queues[guild_id][0]["filename"],
        method="fallback",
        before_options=f"-ss {str(passed_time)}",
        options=f"-af 'volume={volume}'",
    )

    _all_guild_song_queues[guild_id][0]["passed_time"] = timedelta(seconds=0)

    def song_has_ended(_: Exception | None) -> asyncio.Future:
        loops_local = _all_guild_loop_settings.get(guild_id, 0)
        if loops_local == 0:
            try:
                remove_downloaded_song(
                    _all_guild_song_queues.get(guild_id, [None]).pop(0)
                )
            except IndexError:
                pass
        return asyncio.run_coroutine_threadsafe(play_next(ctx), bot.loop)

    if not ctx.voice_client:
        logging.error(
            "Error while trying to start playback, no voice_client was found."
        )
        cleanup(guild_id)
        return

    try:
        ctx.voice_client.play(source, after=song_has_ended)
    except discord.errors.ClientException as exc:
        logging.error("Error while trying to start playback: %s", exc)
        cleanup(guild_id)
        if is_active(ctx):
            ctx.voice_client.stop()
        return

    if _pause_after_play.get(guild_id, False):
        ctx.voice_client.pause()
        _pause_after_play[guild_id] = False


@bot.slash_command(name="leave", description="Leave the voice channel")
@commands.guild_only()
async def leave(ctx: discord.ApplicationContext) -> None:
    if ctx.voice_client and ctx.voice_client.is_connected():
        await ctx.voice_client.disconnect()
        await ctx.respond("Left the voice channel.")
    else:
        await ctx.respond("I am not in a voice channel!", ephemeral=True)


@bot.slash_command(name="volume", description="Adjust the volume")
@option(
    "value",
    description=f"Enter a value between 1 and 100, default is {int(DEFAULT_BOT_VOLUME * 100)}",
    required=False,
    input_type=int,
    min_value=1,
    max_value=100,
)
@commands.guild_only()
async def volume(ctx: discord.ApplicationContext, value: int) -> None:
    current_volume = int(
        (_all_guild_volume_settings.get(ctx.guild_id, DEFAULT_BOT_VOLUME)) * 100
    )

    if not value or value == current_volume:
        await ctx.respond(f"Volume currently is set to {current_volume}%.")
        return

    _all_guild_volume_settings[ctx.guild_id] = float(value) / 100

    try:
        with open(VOLUME_SETTINGS_FILE_PATH, "w") as file:
            json.dump(_all_guild_volume_settings, file, indent=4)
    except (OSError, json.JSONDecodeError) as exc:
        _all_guild_volume_settings[ctx.guild_id] = DEFAULT_BOT_VOLUME
        logging.error(
            "Storing new volume setting for guild '%s' failed: %s", ctx.guild, exc
        )
        await ctx.respond("Changing the volume failed, please try again.")
        return

    # apply volume to playing songs
    if ctx.voice_client:
        try:
            if ctx.voice_client.is_playing():
                now = datetime.now()
                passed_time = now - _all_guild_song_queues[ctx.guild_id][0].get(
                    "starting_time", now
                )
                _all_guild_song_queues[ctx.guild_id][0]["passed_time"] = passed_time
            elif ctx.voice_client.is_paused():
                _all_guild_song_queues[ctx.guild_id][0]["passed_time"] = (
                    _all_guild_song_queues[ctx.guild_id][0].get(
                        "passed_time_until_pause", timedelta(0)
                    )
                )
                _pause_after_play[ctx.guild_id] = True
        except KeyError as exc:
            await ctx.respond(
                "Couldn't apply new volume to current song. New volume will be applied to the next song in queue."
            )
            logging.error("Failed to apply volume to current song: %s", exc)
            return

        if is_active(ctx):
            loops = _all_guild_loop_settings.get(ctx.guild_id, 0)
            _all_guild_loop_settings[ctx.guild_id] = loops + 1 if loops >= 0 else loops
            ctx.voice_client.stop()

    await ctx.respond(f"Changed the volume to {value}%.")


@bot.slash_command(
    name="stop_download",
    description="Stop downloading the playlist (does not stop the current song being downloaded)",
)
@commands.guild_only()
async def stop_downloading(ctx: discord.ApplicationContext) -> None:
    if not _is_downloading_per_guild.get(ctx.guild_id, False):
        await ctx.respond("No songs are being downloaded right now.", ephemeral=True)
        return

    global _stop_downloading_interaction
    _stop_downloading_interaction = await ctx.respond(
        "Trying to stop the download of remaining songs  <a:loading:1373455971296346153>"
    )

    counter = 0
    while counter < 5:
        await asyncio.sleep(1)
        if not _stop_downloading_interaction:
            break
        counter += 1

    # if the download hasn't stopped after 5s, the download probably finished too soon
    if counter == 5:
        await ctx.edit(content="Couldn't stop the download.")


if __name__ == "__main__":
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        logging.error("DISCORD_TOKEN environment variable is not set. Exiting.")
        sys.exit(1)
    bot.run(token)
