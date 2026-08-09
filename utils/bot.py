import asyncio
import contextlib
import logging
import os
import time as _time
from collections.abc import Callable
from datetime import time
from typing import TYPE_CHECKING, TypeVar, cast
from zoneinfo import ZoneInfo

import discord
import discord.voice
import psutil
from discord.channel import VocalGuildChannel
from discord.ext import commands, tasks

from utils.exception_reporter import ExceptionReporter
from utils.observable_set import ObservableSet
from utils.song import Song

if TYPE_CHECKING:
    from cogs.songs import Songs


BOT_REPORTS_CHANNEL_ID = 1403711339355963443
DEFAULT_BOT_VOLUME = 0.2
DOWNLOADS_FOLDER_PATH = "./downloads/"
LOADING_EMOJI_ID = 1373455971296346153
LOCAL_TZ = ZoneInfo("Europe/Berlin")
T = TypeVar("T")
VOLUME_SETTINGS_FILE_PATH = "./persistent/volumesettings.json"

# Timing and magic value constants
DISCONNECTION_COUNTDOWN_SECONDS = 300
MEMORY_INTERVAL_HOURS = 12  # must be 0 < h <= 24
RESTART_ARGS_MIN = 3  # require at least [script, channel_id, message_id]
WATCHDOG_CHECK_INTERVAL = 5
WATCHDOG_TIMEOUT = 15
DOWNLOAD_MESSAGE_INTERVAL = 15
STOP_DOWNLOAD_TIMEOUT_SECONDS = 5
PROCESSING_TIMEOUT_SECONDS = 5
VOICE_CHANNEL_CONNECT_TIMEOUT_SECONDS = 20
DOWNLOAD_SOCKET_TIMEOUT_SECONDS = 15


logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s]: %(message)s",
    handlers=[logging.FileHandler("babshoven.log"), logging.StreamHandler()],
)


class NotOneOfTheBois(commands.CheckFailure):
    """Custom exception raised when a command is used by someone not in the allowed list."""


class Bot(commands.Bot):
    """Custom discord.ext.commands.Bot class to hold additional attributes.

    Holds all shared and per-guild mutable state for the bot instance.

    Attributes:
        exception_reporter: The exception reporter instance, if initialized.
        logger: Logger instance for logging messages.
        watchdog_last_tick: Last time the watchdog was updated.
        stop_downloading_interaction: The current interaction for stopping downloads, if any.
        song_max_length_minutes: Maximum allowed song length in minutes.
        playlist_songs_limit: Maximum allowed songs in a playlist.
        per_guild_volume_settings: Volume settings per guild.
        per_guild_pause_after_play: Pause-after-play flags per guild.
        guild_download_ids: Contains skipped song ids due to download_archive hits, per guild (reset after each /play).
        download_archive: Observable set of downloaded song IDs.
        per_guild_is_downloading: Downloading state per guild.
        per_guild_song_queues: Song queues per guild.
        per_guild_added_song: Most recently added song per guild.
        per_guild_loop_settings: `(guild_id: -n | 0 | +n)` -> `-n`: loop infinite, otherwise `+n` times.
        per_guild_voice_channel_id: Tracks the last known voice channel ID for each guild the bot is connected to.

    """

    def __init__(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
        super().__init__(*args, **kwargs)
        self.exception_reporter: ExceptionReporter | None = None
        self.logger = logging.getLogger(__name__)

        self.watchdog_last_tick = _time.time()
        self.stop_downloading_interaction: discord.Interaction | discord.WebhookMessage | None = None
        self.song_max_length_minutes: int = 60
        self.playlist_songs_limit: int = 50
        self.per_guild_volume_settings: dict[int, float] = {}
        self.per_guild_pause_after_play: dict[int, bool] = {}
        self.guild_download_ids: dict[int, list[str]] = {}
        self.download_archive = ObservableSet()
        self.per_guild_is_downloading: dict[int, bool] = {}
        self.per_guild_song_queues: dict[int, list[Song]] = {}
        self.per_guild_added_song: dict[int, Song] = {}
        self.per_guild_loop_settings: dict[int, int] = {}
        """(guild_id: -n | 0 | +n) -> -n: loop infinite, otherwise +n times"""
        self.per_guild_voice_channel_id: dict[int, int] = {}

    @staticmethod
    def find_dict_by_id(to_search_in: list[Song], song_id: str) -> list[Song]:
        """Find all songs with a matching ID in the provided list."""
        # Filter out empty dicts (error handling), then find by ID
        non_empty = [d for d in to_search_in if d]
        return [d for d in non_empty if d["id"] == song_id]

    @staticmethod
    def is_one_of_the_bois() -> Callable[[T], T]:
        """Check if the command is used by one of the bois."""

        async def predicate(ctx: commands.Context) -> bool:
            if ctx.author.id not in {181388057365315585, 373461017994330112, 580456878472167445}:
                msg = "You can not use this command."
                raise NotOneOfTheBois(msg)
            return True

        return commands.check(predicate)

    @staticmethod
    def create_embed(
        title: str | None = None,
        description: str | None = None,
        color: int | discord.Colour | None = None,
        footer: str | None = None,
    ) -> discord.Embed:
        """Create a standardized Discord embed used across the bot."""
        embed_var = discord.Embed(title=title, description=description, color=color)
        embed_var.set_footer(text=footer)
        return embed_var

    def is_active(self, ctx: discord.ApplicationContext) -> bool:
        """Check if the bot is actively playing or paused in the voice channel."""
        return bool(ctx.voice_client and (ctx.voice_client.is_playing() or ctx.voice_client.is_paused()))

    def cleanup(self, guild_id: int) -> None:
        """Clear song queue, reset loop settings, and remove downloaded files for a guild."""
        guild_queue = self.per_guild_song_queues.get(guild_id, []).copy()
        for song in guild_queue:
            with contextlib.suppress(ValueError):
                self.per_guild_song_queues.get(guild_id, []).remove(song)
            cast("Songs", self.get_cog("Songs")).remove_downloaded_song(song)
        self.per_guild_song_queues.pop(guild_id, None)
        self.per_guild_loop_settings.pop(guild_id, None)

    async def disconnect_countdown(self, channel: VocalGuildChannel) -> None:
        """Wait for guild inactivity and disconnect if lonely."""
        countdown = DISCONNECTION_COUNTDOWN_SECONDS // 10
        while len([member for member in channel.members if not member.bot]) == 0 and countdown > 0:
            countdown -= 1
            await asyncio.sleep(10)

        if countdown == 0:
            vcs = list(
                filter(
                    lambda vc: channel.guild.id == cast("discord.Guild", vc.guild).id,
                    cast("list[discord.voice.VoiceClient]", self.voice_clients),
                )
            )
            if len(vcs) == 0:
                self.logger.info("I tried to leave, but I already was disconnected earlier.")
                return
            self.logger.info("Left the voice channel after feeling lonely.")
            vc: discord.voice.VoiceClient = vcs[0]
            self.cleanup(channel.guild.id)
            if vc.is_playing() or vc.is_paused():
                vc.stop()
            await vc.disconnect()

    def install_asyncio_handler(self, reporter: ExceptionReporter) -> None:
        """Install a custom exception handler for the bot event loop to report exceptions."""

        def handler(loop: asyncio.AbstractEventLoop, context: dict) -> None:
            exception = context.get("exception")

            if exception is None:
                exception = RuntimeError(context["message"])

            loop.create_task(reporter.report(exception, context=context.get("message")))

        self.loop.set_exception_handler(handler)

    @tasks.loop(
        time=tuple(
            time(hour=(i * MEMORY_INTERVAL_HOURS) % 24, tzinfo=LOCAL_TZ) for i in range(24 // MEMORY_INTERVAL_HOURS)
        )
    )
    async def memory_reporter(self, channel: discord.TextChannel, process: psutil.Process) -> None:
        """Report memory and CPU usage to the bot reports channel."""
        mem_mb = process.memory_info().rss / 1024 / 1024
        total_mb = psutil.virtual_memory().total / 1024 / 1024
        cpu_percent = process.cpu_percent(interval=None)
        await channel.send(f"🖥 Memory: {mem_mb:.2f} MB / {total_mb:.0f} MB | CPU: {cpu_percent:.1f}%")

    @tasks.loop(seconds=WATCHDOG_CHECK_INTERVAL)
    async def watchdog_ticker(self) -> None:
        """Update the watchdog ticker to prevent timeout."""
        self.watchdog_last_tick = _time.time()

    def watchdog(self, interval: int = WATCHDOG_CHECK_INTERVAL, timeout: int = WATCHDOG_TIMEOUT) -> None:
        """Monitor the bot's main event loop and forcibly exit if the watchdog ticker is not updated in time.

        Args:
            interval: How often to check the watchdog ticker (seconds).
            timeout: How long to wait before considering the bot frozen (seconds).

        """
        while True:
            _time.sleep(interval)
            if _time.time() - self.watchdog_last_tick > timeout:
                self.logger.error("Bot appears frozen, killing the process...")
                os._exit(1)
