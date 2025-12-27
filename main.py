from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import sys
import threading
import time as _time
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, NotRequired, TypedDict, cast
from zoneinfo import ZoneInfo

import anyio
import discord
import psutil
import yt_dlp
import yt_dlp.utils
from discord import option
from discord.ext import commands, tasks

from observable_set import ObservableSet

if TYPE_CHECKING:
    from discord.channel import VocalGuildChannel

__all__ = ["bot"]


class BotState:
    """Holds all shared and per-guild mutable state for the bot instance.

    Attributes:
        watchdog_last_tick: Last time the watchdog was updated.
        stop_downloading_interaction: The current interaction for stopping downloads, if any.
        song_max_length_minutes: Maximum allowed song length in minutes.
        playlist_songs_limit: Maximum allowed songs in a playlist.
        per_guild_volume_settings: Volume settings per guild.
        per_guild_pause_after_play: Pause-after-play flags per guild.
        guild_download_ids: Song IDs skipped due to download archive, per guild.
        download_archive: Observable set of downloaded song IDs.
        per_guild_is_downloading: Downloading state per guild.
        per_guild_song_queues: Song queues per guild.
        per_guild_added_song: Most recently added song per guild.
        per_guild_loop_settings: Loop settings per guild.
        per_guild_voice_channel_id: Tracks the last known voice channel ID for each guild the bot is connected to.

    """

    def __init__(self) -> None:
        self.watchdog_last_tick = _time.time()
        self.stop_downloading_interaction: discord.Interaction | discord.WebhookMessage | None = None
        self.song_max_length_minutes: int = 60
        self.playlist_songs_limit: int = 50
        self.per_guild_volume_settings: dict[int, float] = {}
        self.per_guild_pause_after_play: dict[int, bool] = {}
        self.guild_download_ids: dict[int, list[str]] = {}
        """contains ids of songs skipped due to download_archive hits (reset after each /play)"""
        self.download_archive: ObservableSet = ObservableSet(logger=logger)
        self.per_guild_is_downloading: dict[int, bool] = {}
        self.per_guild_song_queues: dict[int, list[Song]] = {}
        self.per_guild_added_song: dict[int, Song] = {}
        self.per_guild_loop_settings: dict[int, int] = {}
        """(guild_id: -n | 0 | +n) -> -n: loop infinite, otherwise +n times"""
        self.per_guild_voice_channel_id: dict[int, int] = {}


logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s]: %(message)s",
    handlers=[logging.FileHandler("babshoven.log"), logging.StreamHandler()],
)

logger = logging.getLogger(__name__)

# Constants for bot configuration
BOT_OWNER_ID = 191530044491956224
BOT_REPORTS_CHANNEL_ID = 1403711339355963443
LOADING_EMOJI_ID = 1373455971296346153
VOLUME_SETTINGS_FILE_PATH = "./persistent/volumesettings.json"
DEFAULT_BOT_VOLUME = 0.2


# Timing and magic value constants
DISCONNECTION_COUNTDOWN_SECONDS = 300
MEMORY_INTERVAL_HOURS = 12  # must be 0 < h <= 24
RESTART_ARGS_MIN = 3  # require at least [script, channel_id, message_id]
WATCHDOG_CHECK_INTERVAL = 5
WATCHDOG_TIMEOUT = 15
DOWNLOAD_MESSAGE_INTERVAL = 15
STOP_DOWNLOAD_TIMEOUT_SECONDS = 5
PROCESSING_TIMEOUT_SECONDS = 5
YOUTUBE_CONNECT_TIMEOUT_SECONDS = 2
SONG_DURATION_ONE_MINUTE = 60
SONG_DURATION_ONE_HOUR = 60 * 60


class YTDLPLogger:
    """Logger wrapper for yt_dlp."""

    def __init__(self, guild_id: int) -> None:
        self.logger = logging.getLogger(__name__)
        self.guild_id = guild_id
        self.download_message_interval = DOWNLOAD_MESSAGE_INTERVAL

    def debug(self, msg: str) -> None:
        """Log debug messages and track recorded videos and ETA updates."""
        if "has already been recorded in" in msg:
            bot_state.guild_download_ids.setdefault(self.guild_id, []).append(
                msg.split(":")[0].removeprefix("[download] ")[len("\x1b[0;32m") : -len("\x1b[0m")]
            )
        if "ETA" in msg:
            if self.download_message_interval == DOWNLOAD_MESSAGE_INTERVAL:
                self.logger.info(msg.strip())
            elif self.download_message_interval == 0:
                self.download_message_interval = DOWNLOAD_MESSAGE_INTERVAL + 1
            self.download_message_interval -= 1
        else:
            self.logger.info(msg.strip())

    def info(self, msg: str) -> None:
        """Log info messages from yt_dlp."""
        self.logger.info(msg.strip())

    def warning(self, msg: str) -> None:
        """Log warning messages from yt_dlp."""
        self.logger.warning(msg.strip())

    def error(self, msg: str) -> None:
        """Log error messages from yt_dlp."""
        self.logger.error(msg.strip())

    def critical(self, msg: str) -> None:
        """Log critical messages from yt_dlp."""
        self.logger.critical(msg.strip())


class Song(TypedDict):
    """TypedDict for song metadata."""

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


bot = commands.Bot(owner_id=BOT_OWNER_ID)
bot_state = BotState()


def create_embed(
    title: str | None = None,
    description: str | None = None,
    color: discord.Colour | None = None,
    footer: str | None = None,
) -> discord.Embed:
    """Create a Discord embed with optional title, description, color, and footer."""
    embed = discord.Embed(title=title, description=description, color=color)
    if footer:
        embed.set_footer(text=footer)
    return embed


def cleanup(guild_id: int) -> None:
    """Clear song queue, reset loop settings, and remove downloaded files for a guild."""
    guild_queue = bot_state.per_guild_song_queues.get(guild_id, []).copy()
    for song in guild_queue:
        with contextlib.suppress(ValueError):
            bot_state.per_guild_song_queues.get(guild_id, []).remove(song)
        remove_downloaded_song(song)
    bot_state.per_guild_song_queues.pop(guild_id, None)
    bot_state.per_guild_loop_settings.pop(guild_id, None)


def find_dict_by_id(to_search_in: list[Song], song_id: str) -> list[Song]:
    """Find all songs with a matching ID in the provided list."""
    # Filter out empty dicts (error handling), then find by ID
    non_empty = [d for d in to_search_in if d]
    return [d for d in non_empty if d["id"] == song_id]


async def disconnect_countdown(channel: VocalGuildChannel) -> None:
    """Wait for guild inactivity and disconnect if lonely."""
    countdown = DISCONNECTION_COUNTDOWN_SECONDS // 10
    while len([member for member in channel.members if not member.bot]) == 0 and countdown > 0:
        countdown -= 1
        await asyncio.sleep(10)

    if countdown == 0:
        vcs = list(
            filter(
                lambda vc: channel.guild.id == cast("discord.Guild", vc.guild).id,
                cast("list[discord.VoiceClient]", bot.voice_clients),
            )
        )
        if len(vcs) == 0:
            logger.info("I tried to leave, but I already was disconnected earlier.")
            return
        logger.info("Left the voice channel after feeling lonely.")
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
async def memory_reporter(channel: discord.TextChannel, process: psutil.Process) -> None:
    """Report memory and CPU usage to the bot reports channel."""
    mem_mb = process.memory_info().rss / 1024 / 1024
    total_mb = psutil.virtual_memory().total / 1024 / 1024
    cpu_percent = process.cpu_percent(interval=None)
    await channel.send(f"🖥 Memory: {mem_mb:.2f} MB / {total_mb:.0f} MB | CPU: {cpu_percent:.1f}%")


@tasks.loop(seconds=WATCHDOG_CHECK_INTERVAL)
async def watchdog_ticker() -> None:
    """Update the watchdog ticker to prevent timeout."""
    bot_state.watchdog_last_tick = _time.time()


def watchdog(interval: int = WATCHDOG_CHECK_INTERVAL, timeout: int = WATCHDOG_TIMEOUT) -> None:
    """Monitor the bot's main event loop and forcibly exit if the watchdog ticker is not updated in time.

    Args:
        interval: How often to check the watchdog ticker (seconds).
        timeout: How long to wait before considering the bot frozen (seconds).

    """
    while True:
        _time.sleep(interval)
        if _time.time() - bot_state.watchdog_last_tick > timeout:
            logger.error("Bot appears frozen, killing the process...")
            os._exit(1)


@bot.slash_command(name="ping", description="Check the bot's latency")
async def ping(ctx: discord.ApplicationContext) -> None:
    """Check the bot's latency and respond with the ping time."""
    await ctx.respond(f"Latency: {round(bot.latency * 1000)} ms")


@bot.slash_command(name="restart", description="Restart the bot")
@commands.is_owner()
async def restart(ctx: discord.ApplicationContext) -> None:
    """Restart the bot process with the same command-line arguments."""
    interaction = await ctx.respond("Restarting...")
    response = await cast("discord.Interaction", interaction).original_response()
    os.execv(  # noqa: S606
        sys.executable,
        ["python", *sys.argv, str(response.channel.id), str(response.id)],
    )


@bot.slash_command(name="clear_cache", description="Clear download cache")
@commands.is_owner()
async def clear_cache(ctx: discord.ApplicationContext) -> None:
    """Clear the download archive cache."""
    bot_state.download_archive.clear()
    await ctx.respond("Cleared download cache.")


@bot.slash_command(name="override_limits", description="Override the bot's limits")
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
    ctx: discord.ApplicationContext,
    max_song_length: int | None = None,
    playlist_limit: int | None = None,
) -> None:
    """Override the bot's song and playlist limits."""
    if not (max_song_length or playlist_limit):
        await ctx.respond("You need to specify at least one option.", ephemeral=True)
        return

    await ctx.defer()

    if max_song_length:
        await ctx.respond(
            f"Changed maximum song duration from {bot_state.song_max_length_minutes} to {max_song_length}!"
        )
        bot_state.song_max_length_minutes = max_song_length
    if playlist_limit:
        await ctx.respond(
            f"Changed maximum number of songs per playlist from {bot_state.playlist_songs_limit} to {playlist_limit}!"
        )
        bot_state.playlist_songs_limit = playlist_limit

    for cmd_option in cast("discord.SlashCommand", play).options:
        if cmd_option.name == "playlist_limit":
            cmd_option.description = (
                cmd_option.description.rsplit(" ", 1)[0] + " " + str(bot_state.playlist_songs_limit)
            )
            cmd_option.max_value = bot_state.playlist_songs_limit
            break

    await bot.sync_commands()


@bot.event
async def on_application_command_error(ctx: discord.ApplicationContext, error: discord.DiscordException) -> None:
    """Handle errors from application commands."""
    if isinstance(error, commands.NotOwner):
        await ctx.respond("Sorry, only the bot owner can use this command!", ephemeral=True)
    elif isinstance(error, commands.NoPrivateMessage):
        await ctx.respond("Sorry, this command can't be used in a DM!")
    else:
        logger.error(error)
        raise error


def is_active(ctx: discord.ApplicationContext) -> bool:
    """Check if the bot is actively playing or paused in the voice channel."""
    return bool(ctx.voice_client and (ctx.voice_client.is_playing() or ctx.voice_client.is_paused()))


def current_song_info(ctx: discord.ApplicationContext) -> str:
    """Get formatted information about the currently playing song."""
    if ctx.guild_id is None:
        return ""
    try:
        current_song = bot_state.per_guild_song_queues[ctx.guild_id][0]
    except (KeyError, IndexError):
        return ""

    loops = bot_state.per_guild_loop_settings.get(ctx.guild_id, 0)
    if not current_song or not ctx.voice_client:
        return ""

    header = ""
    if ctx.voice_client.is_playing():
        now = datetime.now()
        runtime = str(now - current_song.get("starting_time", now)).split(".")[0]
    elif ctx.voice_client.is_paused():
        runtime = str(current_song.get("passed_time_until_pause", timedelta(0))).split(".")[0]
        header = "# [Playback is paused]\n"
    else:
        runtime = "0:00:00"

    duration_string = current_song["duration_string"]
    if current_song["duration"] < SONG_DURATION_ONE_MINUTE:
        duration_string = "0:" + duration_string.zfill(2)
    if current_song["duration"] < SONG_DURATION_ONE_HOUR:
        runtime = runtime.removeprefix("0:").removeprefix("0")

    title = current_song["title"]
    link = current_song["song_link"]
    response = f"{header}- **[{title}](<{link}>) - ({runtime} / {duration_string})"

    if loops != 0:
        loop_text = f" [Looped: {loops if loops > 0 else '\u221e'} time{'s' if loops != 1 else ''} left]"
        response += loop_text

    return response + "**"


def remove_downloaded_song(current_song: Song | None) -> None:
    """Delete a downloaded song file if it's no longer in any queue."""
    if not current_song:
        return

    # check if any of the song queues contains the filename
    filename = current_song["filename"]
    all_songs = bot_state.per_guild_song_queues.values()
    if not any(song["filename"] == filename for queue in all_songs for song in queue):
        try:
            Path(filename).unlink()
            logger.info("Deleted %s successfully.", filename)
            bot_state.download_archive.discard(current_song["archive_id"])
        except FileNotFoundError:
            logger.exception("Deleting %s failed, file was not found.", filename)


async def play_next(ctx: discord.ApplicationContext) -> None:  # noqa: C901
    """Play the next song in the queue when the current song finishes."""
    guild_id = ctx.guild_id
    if guild_id is None:
        return
    volume = bot_state.per_guild_volume_settings.get(guild_id, DEFAULT_BOT_VOLUME)
    loops = bot_state.per_guild_loop_settings.get(guild_id, 0)

    if loops == 0 and len(bot_state.per_guild_song_queues.get(guild_id, [])) == 0:
        return
    if loops != 0:
        bot_state.per_guild_loop_settings[guild_id] -= 1

    passed_time = bot_state.per_guild_song_queues[guild_id][0].get("passed_time", timedelta(seconds=0))
    bot_state.per_guild_song_queues[guild_id][0]["starting_time"] = datetime.now() - passed_time

    source = await discord.FFmpegOpusAudio.from_probe(
        bot_state.per_guild_song_queues[guild_id][0]["filename"],
        method="fallback",
        before_options=f"-ss {passed_time!s}",
        options=f"-af 'volume={volume}'",
    )

    bot_state.per_guild_song_queues[guild_id][0]["passed_time"] = timedelta(
        seconds=0
    )  # reset passed_time in case of loops

    def song_has_ended(e: Exception | None) -> None:
        loops = bot_state.per_guild_loop_settings.get(guild_id, 0)
        # try to remove song only if it's not actively being looped
        if loops == 0:
            with contextlib.suppress(IndexError):
                remove_downloaded_song(bot_state.per_guild_song_queues.get(guild_id, [None]).pop(0))

        if e:
            logger.exception("Error after song ended.")

        asyncio.run_coroutine_threadsafe(play_next(ctx), bot.loop)

    if not ctx.voice_client:
        logger.error("Error while trying to start playback, no voice_client was found.")
        cleanup(guild_id)
        return

    try:
        ctx.voice_client.play(source, after=song_has_ended)
    except discord.errors.ClientException:
        logger.exception("Error while trying to start playback.")
        cleanup(guild_id)
        if is_active(ctx):
            ctx.voice_client.stop()
        return

    if bot_state.per_guild_pause_after_play.get(guild_id, False):
        ctx.voice_client.pause()
        bot_state.per_guild_pause_after_play[guild_id] = False


##################################################################


@bot.slash_command(name="leave", description="Leave the voice channel")
@commands.guild_only()
async def leave(ctx: discord.ApplicationContext) -> None:
    """Leave the voice channel."""
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
async def volume(ctx: discord.ApplicationContext, value: int | None = None) -> None:
    """Adjust the playback volume for the guild."""
    if ctx.guild_id is None:
        raise commands.NoPrivateMessage
    current_volume = int((bot_state.per_guild_volume_settings.get(ctx.guild_id, DEFAULT_BOT_VOLUME)) * 100)

    if not value or value == current_volume:
        await ctx.respond(f"Volume currently is set to {current_volume}%.")
        return

    bot_state.per_guild_volume_settings[ctx.guild_id] = float(value) / 100

    try:
        async with await anyio.open_file(VOLUME_SETTINGS_FILE_PATH, "w") as file:
            volume_json = json.dumps(bot_state.per_guild_volume_settings, indent=4)
            await file.write(volume_json)
    except (OSError, json.JSONDecodeError):
        bot_state.per_guild_volume_settings[ctx.guild_id] = DEFAULT_BOT_VOLUME
        logger.exception("Storing new volume setting for guild '%s' failed.", ctx.guild)
        await ctx.respond("Changing the volume failed, please try again.")
        return

    # apply volume to playing songs
    if ctx.voice_client:
        try:
            if ctx.voice_client.is_playing():
                now = datetime.now()
                passed_time = now - bot_state.per_guild_song_queues[ctx.guild_id][0].get("starting_time", now)
                bot_state.per_guild_song_queues[ctx.guild_id][0]["passed_time"] = passed_time
            elif ctx.voice_client.is_paused():
                bot_state.per_guild_song_queues[ctx.guild_id][0]["passed_time"] = bot_state.per_guild_song_queues[
                    ctx.guild_id
                ][0].get("passed_time_until_pause", timedelta(0))
                bot_state.per_guild_pause_after_play[ctx.guild_id] = True
        except KeyError:
            await ctx.respond(
                "Couldn't apply new volume to current song. New volume will be applied to the next song in queue."
            )
            logger.exception("Failed to apply volume to current song.")
            return

        if is_active(ctx):
            loops = bot_state.per_guild_loop_settings.get(ctx.guild_id, 0)
            bot_state.per_guild_loop_settings[ctx.guild_id] = loops + 1 if loops >= 0 else loops
            ctx.voice_client.stop()

    await ctx.respond(f"Changed the volume to {value}%.")


@bot.slash_command(
    name="stop_download",
    description="Stop downloading the playlist (does not stop the current song being downloaded)",
)
@commands.guild_only()
async def stop_downloading(ctx: discord.ApplicationContext) -> None:
    """Stop the download queue for the guild."""
    if ctx.guild_id is None:
        raise commands.NoPrivateMessage

    if not bot_state.per_guild_is_downloading.get(ctx.guild_id, False):
        await ctx.respond("No songs are being downloaded right now.", ephemeral=True)
        return

    bot_state.stop_downloading_interaction = await ctx.respond(
        f"Trying to stop the download of remaining songs  <a:loading:{LOADING_EMOJI_ID}>"
    )

    counter = 0
    while counter < STOP_DOWNLOAD_TIMEOUT_SECONDS:
        await asyncio.sleep(1)
        if not bot_state.stop_downloading_interaction:
            break
        counter += 1

    # if the download hasn't stopped after STOP_DOWNLOAD_TIMEOUT_SECONDS, the download probably finished too soon
    if counter == STOP_DOWNLOAD_TIMEOUT_SECONDS:
        await ctx.edit(content="Couldn't stop the download.")


@bot.slash_command(
    name="play",
    description="Add a YouTube video to the queue or resume paused playback (if all parameters are left empty)",
)
@option("url", description="Link to the YouTube video", required=False, input_type=str)
@option("search_terms", description="Search for a YouTube video", required=False, input_type=str)
@option(
    "playlist_limit",
    description=f"Don't load more than <...> songs for this playlist, default is {bot_state.playlist_songs_limit}",
    required=False,
    input_type=int,
    min_value=1,
    max_value=bot_state.playlist_songs_limit,
)
@commands.guild_only()
async def play(  # noqa: C901, PLR0911, PLR0912, PLR0915
    ctx: discord.ApplicationContext,
    url: str | None = None,
    search_terms: str | None = None,
    playlist_limit: int | None = None,
) -> None:
    """Download and play music."""
    # TODO: refactor this function into smaller parts
    playlist_limit = playlist_limit or bot_state.playlist_songs_limit
    guild_id = ctx.guild_id
    if guild_id is None:
        raise commands.NoPrivateMessage
    counter_for_added_songs = 0
    responded = False  # set to true for ctx.respond's that do not return immediately after
    silent_mode = False  # whether to respond with updates, is turned on when re-downloading songs in a playlist

    bot_state.per_guild_added_song[guild_id] = {
        "archive_id": "",
        "id": "",
        "filename": "",
        "title": "",
        "song_link": "",
        "duration_string": "",
        "duration": 0,
    }

    def add_archive_id(element: str) -> None:
        """Callback to set the archive_id for the most recently added song."""
        bot_state.per_guild_added_song[guild_id]["archive_id"] = element

    bot_state.per_guild_song_queues.setdefault(guild_id, [])
    bot_state.per_guild_volume_settings.setdefault(guild_id, DEFAULT_BOT_VOLUME)
    bot_state.download_archive.set_callback(add_archive_id, overwrite=False)

    if url and search_terms:
        await ctx.respond("Don't use both parameters at the same time.", ephemeral=True)
        return

    if cast("discord.Member", ctx.author).voice:
        channel = cast("discord.VoiceState", cast("discord.Member", ctx.author).voice).channel
        if ctx.voice_client and ctx.voice_client.is_connected():
            if channel and channel != ctx.voice_client.channel:
                if url or search_terms or is_active(ctx):
                    await ctx.voice_client.move_to(channel)
                    if not (url or search_terms):
                        await ctx.respond("Continuing playback in your new voice channel!")
                        return
        elif url or search_terms:
            try:
                await cast("discord.VoiceChannel | discord.StageChannel", channel).connect(timeout=2, reconnect=False)
            except TimeoutError:
                logger.exception("An error occured while connecting to the voice channel.")
                await ctx.respond("I couldn't join your voice channel. Please check my permissions and try again.")
                return
            except Exception:
                logger.exception("An error occured while connecting to the voice channel.")
                await ctx.respond(
                    "Something went wrong. I might not be fully connected to the voice channel."
                    " Please kick or restart me if necesssary and try again."
                )
                return

    else:
        await ctx.respond("You are not in a voice channel!", ephemeral=True)
        return

    if not url and not search_terms:
        if ctx.voice_client and ctx.voice_client.is_paused():
            ctx.voice_client.resume()
            bot_state.per_guild_song_queues[guild_id][0][
                "starting_time"
            ] = datetime.now() - bot_state.per_guild_song_queues[guild_id][0].get(
                "passed_time_until_pause", timedelta(0)
            )
            await ctx.respond("Playback resumed.")
        else:
            await ctx.respond("No audio is currently paused.", ephemeral=True)
        return

    if url and re.search(r"^(?:https?:\/\/(?:www\.)?)?(?:(?:youtube\.com)|(?:youtu\.be))", url) is None:
        await ctx.respond("Currently, only YouTube is supported.", ephemeral=True)
        return

    await ctx.defer()

    download_dict, processing_dict = {}, {}
    downloading_started, processing_started = False, False
    followup_message: bool | discord.WebhookMessage | None = None

    async def download_reporter() -> None:
        """Report download progress to the user via Discord messages."""
        nonlocal downloading_started, followup_message
        message = followup_message or ctx

        if isinstance(followup_message, discord.WebhookMessage):
            await cast("discord.ApplicationContext", message).edit(content="Started downloading the next song!")
        elif followup_message:
            followup_message = await ctx.send_followup("Started downloading the next song!", wait=True)
            message = followup_message
        else:
            await cast("discord.ApplicationContext", message).edit(content="Started downloading!")

        while True:
            await asyncio.sleep(1)
            if download_dict["status"] == "finished":
                break

            total_bytes = download_dict.get("total_bytes", download_dict.get("total_bytes_estimate", 1))
            progress = (
                f"{(download_dict.get('downloaded_bytes', 0) / total_bytes):.0%}" if total_bytes > 1 else "Unknown"
            )
            eta = download_dict.get("eta", 0)
            if eta > 0 and not cast("float", eta).is_integer():
                eta = str(timedelta(seconds=eta))[:-3]
            else:
                eta = str(timedelta(seconds=eta))

            await cast("discord.ApplicationContext", message).edit(
                content="_Downloading song_  <a:loading:1373455971296346153>\n"
                f"- **Progress:** {progress}\n- **Time left (estimate):** {eta}"
                f"\n- **Elapsed time:** {str(timedelta(seconds=download_dict['elapsed'])).split('.')[0]}"
            )
            await asyncio.sleep(1)

        downloading_started = False

    async def processing_reporter() -> None:
        """Report post-download processing progress and update the queue message."""
        nonlocal processing_started, followup_message

        sleep_duration = 0
        while followup_message and not isinstance(followup_message, discord.WebhookMessage):
            await asyncio.sleep(0.1)
            sleep_duration += 0.1
            if sleep_duration >= PROCESSING_TIMEOUT_SECONDS:  # safeguard, don't wait too long in case of bugs/errors
                logger.error("followup_message was never assigned properly. No longer wait for it to change.")
                return

        message = followup_message or ctx

        await message.edit(content=f"Download has finished, finalizing  <a:loading:{LOADING_EMOJI_ID}>")

        while True:
            await asyncio.sleep(1)
            if processing_dict["status"] == "finished" and processing_dict["postprocessor"] == "MoveFiles":
                break
        processing_started = False

        if followup_message:  # Don't print song info if we're at index >= 2 of playlist
            return
        followup_message = True  # if we're calling the download_reporter again, the followup_message should be active

        sleep_duration = 0
        while not bot_state.per_guild_added_song.get(guild_id):
            await asyncio.sleep(0.1)
            sleep_duration += 0.1
            if sleep_duration >= PROCESSING_TIMEOUT_SECONDS:  # safeguard, don't wait too long in case of bugs/errors
                logger.error("added_song wasn't populated in time. No longer wait for it to change.")
                await ctx.edit(content="Error upon adding your song(s) to the queue. Please try again.")
                return

        queue_length = len(bot_state.per_guild_song_queues.get(guild_id, []))
        if queue_length == 1:
            await ctx.edit(
                content=(
                    f"Queue is empty, [{bot_state.per_guild_added_song[guild_id]['title']}]"
                    f"({bot_state.per_guild_added_song[guild_id]['song_link']}) started to play."
                )
            )
        elif queue_length == 0:
            await ctx.edit(content="Error upon adding your song(s) to the queue. Please try again.")
        else:
            await ctx.edit(
                content=(
                    f"[{bot_state.per_guild_added_song[guild_id]['title']}]"
                    f"({bot_state.per_guild_added_song[guild_id]['song_link']}) was added to the queue "
                    f"at position **{len(bot_state.per_guild_song_queues[guild_id])}**."
                )
            )

    def download_hooks(d: dict) -> None:
        """Hook for yt_dlp to report download progress and trigger reporter task."""
        if silent_mode:
            return
        nonlocal downloading_started, download_dict, responded
        download_dict = d
        if d["status"] == "downloading":
            if not downloading_started:
                bot.loop.create_task(download_reporter())
                responded = True
                downloading_started = True

    ydl = None

    def processing_hooks(d: dict) -> None:
        """Hook for yt_dlp to report postprocessing progress and update queue state."""
        nonlocal processing_started, processing_dict, ydl, counter_for_added_songs
        processing_dict = d
        if d["status"] == "started":
            if not processing_started and not silent_mode:
                bot.loop.create_task(processing_reporter())
                processing_started = True
        if d["status"] == "finished" and d["postprocessor"] == "MoveFiles":
            info_dict = d["info_dict"]

            if not info_dict:  # should never happen, but you can't be too careful
                return

            filename = cast("str", cast("yt_dlp.YoutubeDL", ydl).prepare_filename(info_dict))
            mp3_filename = filename.rsplit(".", 1)[0] + ".mp3"

            if not Path(mp3_filename).is_file():
                Path(filename).rename(mp3_filename)

            bot_state.per_guild_added_song[guild_id] = {
                "archive_id": "",
                "id": cast("str", info_dict.get("id", "")),
                "filename": mp3_filename,
                "title": cast("str", info_dict.get("title", "")),
                "song_link": cast("str", info_dict.get("webpage_url", "")),
                "duration_string": cast("str", info_dict.get("duration_string", "")),
                "duration": cast("int", info_dict.get("duration", 0)),
            }

            bot_state.per_guild_song_queues[guild_id].append(bot_state.per_guild_added_song[guild_id])
            counter_for_added_songs += 1
            if not is_active(ctx):
                bot.loop.create_task(play_next(ctx))

    def download_control(info_dict: dict, *, _: bool) -> str | None:
        """Filter function for yt_dlp to skip songs that are too long or if a stop is requested."""
        duration = info_dict.get("duration")
        if duration and duration > bot_state.song_max_length_minutes * 60:
            return f"'{info_dict.get('title')}' is too long"
        if bot_state.stop_downloading_interaction:
            cancel_msg = "Stop the downloads!"
            raise yt_dlp.utils.DownloadCancelled(cancel_msg)
        return None

    ydl_opts = {
        "download_archive": bot_state.download_archive,
        "format": "bestaudio/best",
        "ignoreerrors": True,
        "logger": YTDLPLogger(guild_id),
        "match_filter": lambda info_dict, incomplete: download_control(info_dict, _=incomplete),
        "noplaylist": bool(search_terms),
        "paths": {"home": "downloads/"},
        "playlist_items": str(list(range(playlist_limit + 1))).replace(" ", "")[1:-1],
        "postprocessor_hooks": [processing_hooks],
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }
        ],
        "progress_hooks": [download_hooks],
        "js_runtimes": {"deno": {"path": os.environ.get("DENO_PATH", "deno")}},
        #'verbose': True,  # noqa: ERA001
        #'ratelimit': 250000,  # noqa: ERA001
    }

    def download_songs(_url: str | None = url) -> object:
        """Download or extract song info using yt_dlp with the current options."""
        nonlocal ydl
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:  # type: ignore[arg-type]
            return ydl.extract_info(_url or f"ytsearch:{search_terms}")

    info_dict = None

    was_cancelled = False
    try:
        bot_state.per_guild_is_downloading[guild_id] = True
        info_dict = await asyncio.to_thread(download_songs)
    except yt_dlp.utils.DownloadCancelled:
        message = bot_state.stop_downloading_interaction
        bot_state.stop_downloading_interaction = None
        await cast("discord.Interaction | discord.WebhookMessage", message).edit(
            content="Stopped downloading the remaining song(s)!"
        )
        was_cancelled = True
    finally:
        bot_state.per_guild_is_downloading[guild_id] = False

    already_downloaded = bot_state.guild_download_ids.setdefault(guild_id, [])
    all_songs = bot_state.per_guild_song_queues[guild_id]
    add_to_queue = []
    for song_id in already_downloaded:
        try:
            add_to_queue.append(find_dict_by_id(all_songs, song_id)[0])
        except IndexError:
            logger.exception(
                "Tried to add %s to the queue, but couldn't find it in the list.",
                song_id,
            )

    del bot_state.guild_download_ids[guild_id]

    was_error = True
    for song in add_to_queue:
        if song.get("archive_id") in bot_state.download_archive:  # song is still present in the downloads
            bot_state.per_guild_song_queues[guild_id].append(song)
            counter_for_added_songs += 1
            was_error = False
        else:  # song is not downloaded anymore by the time execution arrived here, re-download it
            try:
                silent_mode = True
                await asyncio.to_thread(download_songs, f"https://www.youtube.com/watch?v={song['id']}")
                was_error = False
            except yt_dlp.utils.DownloadError:
                logger.exception("Download of song failed during re-download: %s", song["title"])
                continue

    playlist_count = 0

    if not isinstance(info_dict, dict):
        info_dict = None

    if info_dict:
        playlist_count = info_dict.get("playlist_count") or len(info_dict.get("entries", []))
    if was_cancelled or playlist_count > 1:
        response = (
            f"Finished downloading the playlist. {counter_for_added_songs}"
            f"{'' if was_cancelled else (f' / {playlist_count}')} "
            "songs were added to the queue."
        )
        if not was_cancelled and counter_for_added_songs < playlist_count and counter_for_added_songs < playlist_limit:
            response += (
                f"\n\nAn error occurred. Make sure that no song is longer than "
                f"**{bot_state.song_max_length_minutes} minutes or age-restricted**, and try again."
            )
        if isinstance(
            followup_message, discord.WebhookMessage
        ):  # responded to its initial message earlier in the download process, edit the response
            await cast("discord.WebhookMessage", followup_message).edit(content=response)
        else:
            await ctx.respond(response)
        return

    if not responded:
        if not was_error and len(add_to_queue) > 0:
            queue_length = len(bot_state.per_guild_song_queues[guild_id])
            if queue_length == 1:
                await ctx.edit(
                    content=(
                        f"Queue is empty, [{add_to_queue[0]['title']}]({add_to_queue[0]['song_link']}) started to play."
                    )
                )
            else:
                await ctx.respond(
                    f"[{add_to_queue[0]['title']}]({add_to_queue[0]['song_link']}) "
                    f"was added to the queue at position **{queue_length}**."
                )
            return
        await ctx.respond(
            "There were errors downloading your song(s). Please try again, and make sure that no song is longer "
            f"than **{bot_state.song_max_length_minutes} minutes or age-restricted**."
        )


@bot.slash_command(name="loop", description="Loop the current song or stop the loop")
@option(
    "max_times",
    description="Maximum number of times this song will be looped; infinite or 0 if omitted (depends on state)",
    required=False,
    input_type=int,
    min_value=1,
)
@commands.guild_only()
async def loop(ctx: discord.ApplicationContext, max_times: int) -> None:
    """Set or clear looping for the current song in the guild."""
    if ctx.guild_id is None:
        raise commands.NoPrivateMessage

    if not is_active(ctx):
        await ctx.respond("There is nothing to loop.", ephemeral=True)
        return

    loops = bot_state.per_guild_loop_settings.get(ctx.guild_id, 0)
    if loops == 0:
        bot_state.per_guild_loop_settings[ctx.guild_id] = max_times or -1
        await ctx.respond(
            f"The song that is currently played will be looped "
            f"{f'{max_times} time{"s" if max_times > 1 else ""}' if max_times else 'infinitely'}."
        )
    else:
        bot_state.per_guild_loop_settings[ctx.guild_id] = max_times or 0
        await ctx.respond(
            f"Song will be looped {max_times} more time{'s' if max_times > 1 else ''}."
            if max_times
            else "Disabled looping for this song."
        )


@bot.slash_command(name="info", description="Infos about the current song")
@commands.guild_only()
async def info(ctx: discord.ApplicationContext) -> None:
    """Display information about the currently playing song."""
    if not is_active(ctx):
        await ctx.respond("There is currently no song playing.")
        return

    response = current_song_info(ctx)
    if response == "":
        await ctx.respond("Error while retrieving song info. Please try again.", ephemeral=True)
    else:
        await ctx.respond(response)


@bot.slash_command(name="queue", description="Details about the currently playing song and the queue")
@commands.guild_only()
async def queue(ctx: discord.ApplicationContext) -> None:
    """Display the current song and upcoming songs in the queue."""
    if ctx.guild_id is None:
        raise commands.NoPrivateMessage

    if not is_active(ctx):
        await ctx.respond("There are currently no songs in queue.")
        return

    cutoff = 5

    response = current_song_info(ctx) + "\n"

    for i in range(1, len(bot_state.per_guild_song_queues[ctx.guild_id])):
        song = bot_state.per_guild_song_queues[ctx.guild_id][i]

        if i == cutoff:
            response += f"- ...{len(bot_state.per_guild_song_queues[ctx.guild_id]) - cutoff} more song(s).\n"
            break

        duration_string = song["duration_string"]
        if song["duration"] < SONG_DURATION_ONE_MINUTE:
            duration_string = "0:" + duration_string.zfill(2)
        if song["duration"] < SONG_DURATION_ONE_HOUR:
            placeholder = "0:00"
        else:
            placeholder = "0:00:00"

        response += f"- [{song['title']}](<{song['song_link']}>) - ({placeholder} / {duration_string})\n"

    if bot_state.per_guild_is_downloading.get(ctx.guild_id, False):
        response += "\n..._more songs are currently being downloaded_..."

    await ctx.respond(response)


@bot.slash_command(name="clear_queue", description="Stop playback and clear entire queue")
@commands.guild_only()
async def clear_queue(ctx: discord.ApplicationContext) -> None:
    """Stop playback and clear the entire song queue for the guild."""
    if ctx.guild_id is None:
        raise commands.NoPrivateMessage

    if not is_active(ctx):
        await ctx.respond("Queue is already empty.", ephemeral=True)
        return

    cleanup(ctx.guild_id)

    if not ctx.voice_client:
        logger.error("Error when clearing queue, no voice_client was found.")
        await ctx.respond("Something went wrong.")
        return
    ctx.voice_client.stop()

    await ctx.respond("Stopped playback and cleared the queue.")


@bot.slash_command(name="skip", description="Skip the current song")
@commands.guild_only()
async def skip(ctx: discord.ApplicationContext) -> None:
    """Skip the current song in the guild's queue."""
    if ctx.guild_id is None:
        raise commands.NoPrivateMessage

    if is_active(ctx):
        bot_state.per_guild_loop_settings[ctx.guild_id] = 0

        if not ctx.voice_client:
            logger.error("Error when skipping song, no voice_client was found.")
            await ctx.respond("Something went wrong.")
            return
        ctx.voice_client.stop()
        await ctx.respond("Song skipped.")
    else:
        await ctx.respond("No audio is currently playing.", ephemeral=True)


@bot.slash_command(name="pause", description="Pause the current playback")
@commands.guild_only()
async def pause(ctx: discord.ApplicationContext) -> None:
    """Pause the current song playback."""
    if ctx.guild_id is None:
        raise commands.NoPrivateMessage

    if ctx.voice_client and ctx.voice_client.is_playing():
        ctx.voice_client.pause()
        now = datetime.now()
        bot_state.per_guild_song_queues[ctx.guild_id][0]["passed_time_until_pause"] = (
            now - bot_state.per_guild_song_queues[ctx.guild_id][0].get("starting_time", now)
        )
        await ctx.respond("Playback paused.")
    else:
        await ctx.respond("No audio is currently playing.", ephemeral=True)


##################################################################


@bot.listen()
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState) -> None:
    """Handle changes in voice state to manage disconnects and channel tracking."""
    if after.channel:
        if member == bot.user:
            bot_state.per_guild_voice_channel_id[after.channel.guild.id] = after.channel.id
            return

    if not after.channel and member == bot.user:
        guild_id = cast("discord.VoiceChannel | discord.StageChannel", before.channel).guild.id
        cleanup(guild_id)
        return

    if (
        before.channel
        and before.channel.id == bot_state.per_guild_voice_channel_id.get(before.channel.guild.id, None)
        and len([member for member in before.channel.members if not member.bot]) == 0
    ):
        bot.loop.create_task(disconnect_countdown(before.channel))


@bot.listen(once=True)
async def on_ready() -> None:
    """Initialize the bot, load volume settings, and start background tasks."""
    try:
        async with await anyio.open_file(VOLUME_SETTINGS_FILE_PATH, "r") as file:
            bot_state.per_guild_volume_settings = json.loads(
                await file.read(),
                object_pairs_hook=lambda pairs: {int(k): v for k, v in pairs},
            )
    except (OSError, json.JSONDecodeError):
        logger.exception("Error upon reading %s", VOLUME_SETTINGS_FILE_PATH)

    for file in Path("downloads/").glob("*"):
        file.unlink(missing_ok=True)
        logger.info("Deleted leftover file %s successfully.", file)

    logger.info("Logged in as %s", bot.user)

    memory_reporter.start(bot.get_channel(BOT_REPORTS_CHANNEL_ID), psutil.Process(os.getpid()))

    watchdog_ticker.start()
    threading.Thread(target=watchdog, daemon=True).start()

    await bot.wait_until_ready()
    await cast("discord.TextChannel", bot.get_channel(BOT_REPORTS_CHANNEL_ID)).send(
        ":arrows_counterclockwise: Finished restarting!"
    )

    # Called after bot was restarted via command
    if len(sys.argv) >= RESTART_ARGS_MIN:
        channel = bot.get_channel(int(sys.argv[1]))
        msg = await cast("discord.TextChannel", channel).fetch_message(int(sys.argv[2]))
        await msg.edit(content="Restart has finished, I'm back!")


if __name__ == "__main__":
    try:
        token = os.environ.get("DISCORD_TOKEN")
        if not token:
            logger.error("DISCORD_TOKEN environment variable is not set. Exiting.")
            sys.exit(1)
        bot.run(token)
    except Exception:
        logger.exception("Fatal error in outer run loop!")
        sys.exit(1)

# TODO: main in einzelteile aufteilen; neuen command der erlaubt dass auch andere user commands wie restart
# ausfuehren koennen, eine möglichkeit userids zu übergeben
