import asyncio
import contextlib
import json
import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import cast

import anyio
import discord
import yt_dlp
import yt_dlp.utils
from discord.ext import commands

from utils.bot import (
    DEFAULT_BOT_VOLUME,
    DOWNLOAD_SOCKET_TIMEOUT_SECONDS,
    DOWNLOADS_FOLDER_PATH,
    LOADING_EMOJI_ID,
    PROCESSING_TIMEOUT_SECONDS,
    STOP_DOWNLOAD_TIMEOUT_SECONDS,
    VOICE_CHANNEL_CONNECT_TIMEOUT_SECONDS,
    VOLUME_SETTINGS_FILE_PATH,
    Bot,
)
from utils.song import Song
from utils.ytdlp_logger import YTDLPLogger

DEFAULT_PLAYLIST_SONGS_LIMIT = 50
ONE_MINUTE = 60
ONE_HOUR = 60 * 60


class Songs(commands.Cog):
    """Cog for song commands."""

    def __init__(self, bot: Bot) -> None:
        self.bot = bot

    def current_song_info(self, ctx: discord.ApplicationContext) -> str:
        """Get formatted information about the currently playing song."""
        if ctx.guild_id is None:
            return ""
        try:
            current_song = self.bot.per_guild_song_queues[ctx.guild_id][0]
        except (KeyError, IndexError):
            return ""

        loops = self.bot.per_guild_loop_settings.get(ctx.guild_id, 0)
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
        if current_song["duration"] < ONE_MINUTE:
            duration_string = "0:" + duration_string.zfill(2)
        if current_song["duration"] < ONE_HOUR:
            runtime = runtime.removeprefix("0:").removeprefix("0")

        title = current_song["title"]
        link = current_song["song_link"]
        response = f"{header}- **[{title}](<{link}>) - ({runtime} / {duration_string})"

        if loops != 0:
            loop_text = f" [Looped: {loops if loops > 0 else '\u221e'} time{'s' if loops != 1 else ''} left]"
            response += loop_text

        return response + "**"

    def remove_downloaded_song(self, song: Song | None) -> None:
        """Delete a downloaded song file if it's no longer in any queue."""
        if not song:
            return

        # check if any of the song queues contains the filename
        filename = song["filename"]
        all_songs = self.bot.per_guild_song_queues.values()
        if not any(song["filename"] == filename for queue in all_songs for song in queue):
            try:
                Path(filename).unlink()
                self.bot.logger.info("Deleted %s successfully.", filename)
                # TODO: debug purposes, find out what causes the bug of an empty song trying to be removed
                self.bot.logger.info("DEBUG: Current song being removed: %s", song)
                self.bot.logger.info("DEBUG: Current download archive before removal: %s", self.bot.download_archive)
                self.bot.download_archive.discard(song["archive_id"])
            except FileNotFoundError:
                self.bot.logger.exception("Deleting %s failed, file was not found.", filename)

    async def play_next(self, ctx: discord.ApplicationContext) -> None:  # noqa: C901
        """Play the next song in the queue when the current song finishes."""
        guild_id = ctx.guild_id
        if guild_id is None:
            return
        volume = self.bot.per_guild_volume_settings.get(guild_id, DEFAULT_BOT_VOLUME)
        loops = self.bot.per_guild_loop_settings.get(guild_id, 0)

        if loops == 0 and len(self.bot.per_guild_song_queues.get(guild_id, [])) == 0:
            return
        if loops != 0:
            self.bot.per_guild_loop_settings[guild_id] -= 1

        passed_time = self.bot.per_guild_song_queues[guild_id][0].get("passed_time", timedelta(seconds=0))
        self.bot.per_guild_song_queues[guild_id][0]["starting_time"] = datetime.now() - passed_time

        source = discord.PCMVolumeTransformer(
            discord.FFmpegPCMAudio(
                self.bot.per_guild_song_queues[guild_id][0]["filename"],
                before_options=f"-ss {passed_time!s}",
                options="-vn",
            ),
            volume=volume,
        )

        self.bot.per_guild_song_queues[guild_id][0]["passed_time"] = timedelta(
            seconds=0
        )  # reset passed_time in case of loops

        def song_has_ended(e: Exception | None) -> None:
            loops = self.bot.per_guild_loop_settings.get(guild_id, 0)
            # try to remove song only if it's not actively being looped
            if loops == 0:
                with contextlib.suppress(IndexError):
                    self.remove_downloaded_song(self.bot.per_guild_song_queues.get(guild_id, [None]).pop(0))

            if e:
                self.bot.logger.exception("Error after song ended.")

            asyncio.run_coroutine_threadsafe(self.play_next(ctx), self.bot.loop)

        if not ctx.voice_client:
            self.bot.logger.error("Error while trying to start playback, no voice_client was found.")
            await cast("discord.TextChannel", ctx.channel).send(
                "An error occurred while trying to play the next song, clearing song queue."
            )
            self.bot.cleanup(guild_id)
            return

        try:
            ctx.voice_client.play(source, after=song_has_ended)
        except discord.errors.ClientException as e:
            msg = "Error while trying to start playback."
            self.bot.logger.exception(msg)
            if self.bot.exception_reporter:
                await self.bot.exception_reporter.report(e, context=msg)

            await cast("discord.TextChannel", ctx.channel).send(
                "An error occurred while trying to play the next song, clearing song queue."
            )
            self.bot.cleanup(guild_id)
            if self.bot.is_active(ctx):
                ctx.voice_client.stop()
            return

        if self.bot.per_guild_pause_after_play.get(guild_id, False):
            ctx.voice_client.pause()
            self.bot.per_guild_pause_after_play[guild_id] = False

    @commands.slash_command(name="volume", description="Adjust the volume")
    @discord.option(
        "value",
        description=f"Enter a value between 1 and 100, default is {int(DEFAULT_BOT_VOLUME * 100)}",
        required=False,
        input_type=int,
        min_value=1,
        max_value=100,
    )
    @commands.guild_only()
    async def volume(self, ctx: discord.ApplicationContext, value: int | None = None) -> None:
        """Adjust the playback volume for the guild."""
        if ctx.guild_id is None:
            raise commands.NoPrivateMessage
        current_volume = int((self.bot.per_guild_volume_settings.get(ctx.guild_id, DEFAULT_BOT_VOLUME)) * 100)

        if not value or value == current_volume:
            await ctx.respond(f"Volume currently is set to {current_volume}%.")
            return

        self.bot.per_guild_volume_settings[ctx.guild_id] = float(value) / 100

        try:
            async with await anyio.open_file(VOLUME_SETTINGS_FILE_PATH, "w") as file:
                volume_json = json.dumps(self.bot.per_guild_volume_settings, indent=4)
                await file.write(volume_json)
        except (OSError, json.JSONDecodeError):
            self.bot.per_guild_volume_settings[ctx.guild_id] = DEFAULT_BOT_VOLUME
            self.bot.logger.exception("Storing new volume setting for guild '%s' failed.", ctx.guild)
            await ctx.respond("Changing the volume failed, please try again.")
            return

        # apply volume to playing songs
        if ctx.voice_client:
            try:
                if ctx.voice_client.is_playing():
                    now = datetime.now()
                    passed_time = now - self.bot.per_guild_song_queues[ctx.guild_id][0].get("starting_time", now)
                    self.bot.per_guild_song_queues[ctx.guild_id][0]["passed_time"] = passed_time
                elif ctx.voice_client.is_paused():
                    self.bot.per_guild_song_queues[ctx.guild_id][0]["passed_time"] = self.bot.per_guild_song_queues[
                        ctx.guild_id
                    ][0].get("passed_time_until_pause", timedelta(0))
                    self.bot.per_guild_pause_after_play[ctx.guild_id] = True
            except KeyError:
                await ctx.respond(
                    "Couldn't apply new volume to current song. New volume will be applied to the next song in queue."
                )
                self.bot.logger.exception("Failed to apply volume to current song.")
                return

            if self.bot.is_active(ctx):
                loops = self.bot.per_guild_loop_settings.get(ctx.guild_id, 0)
                self.bot.per_guild_loop_settings[ctx.guild_id] = loops + 1 if loops >= 0 else loops
                ctx.voice_client.stop()

        await ctx.respond(f"Changed the volume to {value}%.")

    @commands.slash_command(
        name="stop_download",
        description="Stop downloading the playlist (does not stop the current song being downloaded)",
    )
    @commands.guild_only()
    async def stop_downloading(self, ctx: discord.ApplicationContext) -> None:
        """Stop the download queue for the guild."""
        if ctx.guild_id is None:
            raise commands.NoPrivateMessage

        if not self.bot.per_guild_is_downloading.get(ctx.guild_id, False):
            await ctx.respond("No songs are being downloaded right now.", ephemeral=True)
            return

        self.bot.stop_downloading_interaction = await ctx.respond(
            f"Trying to stop the download of remaining songs  <a:loading:{LOADING_EMOJI_ID}>"
        )

        counter = 0
        while counter < STOP_DOWNLOAD_TIMEOUT_SECONDS:
            await asyncio.sleep(1)
            if not self.bot.stop_downloading_interaction:
                break
            counter += 1

        # if the download hasn't stopped after STOP_DOWNLOAD_TIMEOUT_SECONDS, the download probably finished too soon
        if counter == STOP_DOWNLOAD_TIMEOUT_SECONDS:
            await ctx.edit(content="Couldn't stop the download.")

    @commands.slash_command(
        name="play",
        description="Add a YouTube video to the queue or resume paused playback (if all parameters are left empty)",
    )
    @discord.option("url", description="Link to the YouTube video", required=False, input_type=str)
    @discord.option("search_terms", description="Search for a YouTube video", required=False, input_type=str)
    @discord.option(
        "playlist_limit",
        description=(f"Don't load more than <...> songs for this playlist, default is {DEFAULT_PLAYLIST_SONGS_LIMIT}"),
        required=False,
        input_type=int,
        min_value=1,
        max_value=DEFAULT_PLAYLIST_SONGS_LIMIT,
    )
    @commands.guild_only()
    async def play(  # noqa: C901, PLR0911, PLR0912, PLR0915
        self,
        ctx: discord.ApplicationContext,
        url: str | None = None,
        search_terms: str | None = None,
        playlist_limit: int | None = None,
    ) -> None:
        """Download and play music."""
        # TODO: refactor this function into smaller parts
        playlist_limit = playlist_limit or self.bot.playlist_songs_limit
        guild_id = ctx.guild_id
        if guild_id is None:
            raise commands.NoPrivateMessage
        counter_for_added_songs = 0
        responded = False  # set to true for ctx.respond's that do not return immediately after
        silent_mode = False  # whether to respond with updates, is turned on when re-downloading songs in a playlist

        self.bot.per_guild_added_song[guild_id] = {
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
            self.bot.per_guild_added_song[guild_id]["archive_id"] = element

        self.bot.per_guild_song_queues.setdefault(guild_id, [])
        self.bot.per_guild_volume_settings.setdefault(guild_id, DEFAULT_BOT_VOLUME)
        self.bot.download_archive.set_callback(add_archive_id, overwrite=False)

        if url and search_terms:
            await ctx.respond("Don't use both parameters at the same time.", ephemeral=True)
            return

        if cast("discord.Member", ctx.author).voice:
            channel = cast("discord.VoiceState", cast("discord.Member", ctx.author).voice).channel
            if ctx.voice_client and ctx.voice_client.is_connected():
                if channel and channel != ctx.voice_client.channel:
                    if url or search_terms or self.bot.is_active(ctx):
                        await ctx.voice_client.move_to(channel)
                        if not (url or search_terms):
                            await ctx.respond("Continuing playback in your new voice channel!")
                            return
            elif url or search_terms:
                await ctx.defer()
                try:
                    await cast("discord.VoiceChannel | discord.StageChannel", channel).connect(
                        timeout=VOICE_CHANNEL_CONNECT_TIMEOUT_SECONDS,
                        reconnect=False,
                    )
                except TimeoutError as e:
                    msg = "An error occured while connecting to the voice channel."
                    self.bot.logger.exception(msg)
                    if self.bot.exception_reporter:
                        await self.bot.exception_reporter.report(e, context=msg)

                    await ctx.respond(
                        "I am not fully connected to the voice channel. Please check my permissions and try again."
                    )
                    return
                except Exception as e:
                    msg = "An error occured while connecting to the voice channel."
                    self.bot.logger.exception(msg)
                    if self.bot.exception_reporter:
                        await self.bot.exception_reporter.report(e, context=msg)

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
                self.bot.per_guild_song_queues[guild_id][0][
                    "starting_time"
                ] = datetime.now() - self.bot.per_guild_song_queues[guild_id][0].get(
                    "passed_time_until_pause", timedelta(0)
                )
                await ctx.respond("Playback resumed.")
            else:
                await ctx.respond("No audio is currently paused.", ephemeral=True)
            return

        if url and re.search(r"^(?:https?:\/\/(?:www\.)?)?(?:(?:youtube\.com)|(?:youtu\.be))", url) is None:
            await ctx.respond("Currently, only YouTube is supported.", ephemeral=True)
            return

        with contextlib.suppress(discord.errors.InteractionResponded):
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
                if (
                    sleep_duration >= PROCESSING_TIMEOUT_SECONDS
                ):  # safeguard, don't wait too long in case of bugs/errors
                    self.bot.logger.error(
                        "followup_message was never assigned properly. No longer wait for it to change."
                    )
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
            followup_message = (
                True  # if we're calling the download_reporter again, the followup_message should be active
            )

            sleep_duration = 0
            while not self.bot.per_guild_added_song.get(guild_id):
                await asyncio.sleep(0.1)
                sleep_duration += 0.1
                if (
                    sleep_duration >= PROCESSING_TIMEOUT_SECONDS
                ):  # safeguard, don't wait too long in case of bugs/errors
                    self.bot.logger.error("added_song wasn't populated in time. No longer wait for it to change.")
                    await ctx.edit(content="Error upon adding your song(s) to the queue. Please try again.")
                    return

            queue_length = len(self.bot.per_guild_song_queues.get(guild_id, []))
            if queue_length == 1:
                await ctx.edit(
                    content=(
                        f"Queue is empty, [{self.bot.per_guild_added_song[guild_id]['title']}]"
                        f"({self.bot.per_guild_added_song[guild_id]['song_link']}) started to play."
                    )
                )
            elif queue_length == 0:
                await ctx.edit(content="Error upon adding your song(s) to the queue. Please try again.")
            else:
                await ctx.edit(
                    content=(
                        f"[{self.bot.per_guild_added_song[guild_id]['title']}]"
                        f"({self.bot.per_guild_added_song[guild_id]['song_link']}) was added to the queue "
                        f"at position **{len(self.bot.per_guild_song_queues[guild_id])}**."
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
                    self.bot.loop.create_task(download_reporter())
                    responded = True
                    downloading_started = True

        ydl = None

        def processing_hooks(d: dict) -> None:
            """Hook for yt_dlp to report postprocessing progress and update queue state."""
            nonlocal processing_started, processing_dict, ydl, counter_for_added_songs
            processing_dict = d
            if d["status"] == "started":
                if not processing_started and not silent_mode:
                    self.bot.loop.create_task(processing_reporter())
                    processing_started = True
            if d["status"] == "finished" and d["postprocessor"] == "MoveFiles":
                info_dict = d["info_dict"]

                if not info_dict:  # should never happen, but you can't be too careful
                    return

                filename = cast("str", cast("yt_dlp.YoutubeDL", ydl).prepare_filename(info_dict))
                mp3_filename = filename.rsplit(".", 1)[0] + ".mp3"

                if not Path(mp3_filename).is_file():
                    Path(filename).rename(mp3_filename)

                self.bot.per_guild_added_song[guild_id] = {
                    "archive_id": "",
                    "id": cast("str", info_dict.get("id", "")),
                    "filename": mp3_filename,
                    "title": cast("str", info_dict.get("title", "")),
                    "song_link": cast("str", info_dict.get("webpage_url", "")),
                    "duration_string": cast("str", info_dict.get("duration_string", "")),
                    "duration": cast("int", info_dict.get("duration", 0)),
                }

                self.bot.per_guild_song_queues[guild_id].append(self.bot.per_guild_added_song[guild_id])
                counter_for_added_songs += 1
                if not self.bot.is_active(ctx):
                    self.bot.loop.create_task(self.play_next(ctx))

        def download_control(info_dict: dict, *, _: bool) -> str | None:
            """Filter function for yt_dlp to skip songs that are too long or if a stop is requested."""
            duration = info_dict.get("duration")
            if duration and duration > self.bot.song_max_length_minutes * 60:
                return f"'{info_dict.get('title')}' is too long"
            if self.bot.stop_downloading_interaction:
                cancel_msg = "Stop the downloads!"
                raise yt_dlp.utils.DownloadCancelled(cancel_msg)
            return None

        ydl_opts = {
            "download_archive": self.bot.download_archive,
            "extractor_args": {"youtube": {"player_client": ["android", "web"]}},
            "format": "bestaudio/best",
            "ignoreerrors": True,
            "js_runtimes": {"deno": {"path": os.environ.get("DENO_PATH", "deno")}},
            "logger": YTDLPLogger(self.bot, guild_id),
            "match_filter": lambda info_dict, incomplete: download_control(info_dict, _=incomplete),
            "noplaylist": bool(search_terms),
            "paths": {"home": DOWNLOADS_FOLDER_PATH},
            "playlist_items": str(list(range(playlist_limit + 1))).replace(" ", "")[1:-1],
            "postprocessor_hooks": [processing_hooks],
            "progress_hooks": [download_hooks],
            "socket_timeout": DOWNLOAD_SOCKET_TIMEOUT_SECONDS,
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
            self.bot.per_guild_is_downloading[guild_id] = True
            info_dict = await asyncio.to_thread(download_songs)
        except yt_dlp.utils.DownloadCancelled:
            message = self.bot.stop_downloading_interaction
            self.bot.stop_downloading_interaction = None
            await cast("discord.Interaction | discord.WebhookMessage", message).edit(
                content="Stopped downloading the remaining song(s)!"
            )
            was_cancelled = True
        finally:
            self.bot.per_guild_is_downloading[guild_id] = False

        already_downloaded = self.bot.guild_download_ids.setdefault(guild_id, [])
        all_songs = self.bot.per_guild_song_queues[guild_id]
        add_to_queue = []
        for song_id in already_downloaded:
            try:
                add_to_queue.append(Bot.find_dict_by_id(all_songs, song_id)[0])
            except IndexError as e:
                msg = f"Tried to re-add {song_id} to the queue, but couldn't find it in the download archive."
                self.bot.logger.exception(msg)
                if self.bot.exception_reporter:
                    await self.bot.exception_reporter.report(e, context=msg)

        del self.bot.guild_download_ids[guild_id]

        was_error = True
        for song in add_to_queue:
            if song.get("archive_id") in self.bot.download_archive:  # song is still present in the downloads
                self.bot.per_guild_song_queues[guild_id].append(song)
                counter_for_added_songs += 1
                was_error = False
            else:  # song is not downloaded anymore by the time execution arrived here, re-download it
                try:
                    silent_mode = True
                    await asyncio.to_thread(download_songs, f"https://www.youtube.com/watch?v={song['id']}")
                    was_error = False
                except yt_dlp.utils.DownloadError:
                    self.bot.logger.exception("Download of song failed during re-download: %s", song["title"])
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
            if (
                not was_cancelled
                and counter_for_added_songs < playlist_count
                and counter_for_added_songs < playlist_limit
            ):
                response += (
                    f"\n\nAn error occurred. Make sure that no song is longer than "
                    f"**{self.bot.song_max_length_minutes} minutes or age-restricted**, and try again."
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
                queue_length = len(self.bot.per_guild_song_queues[guild_id])
                if queue_length == 1:
                    await ctx.edit(
                        content=(
                            f"Queue is empty, [{add_to_queue[0]['title']}]({add_to_queue[0]['song_link']})"
                            " started to play."
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
                f"than **{self.bot.song_max_length_minutes} minutes or age-restricted**. "
                "Consider clearing the download cache."
            )

    @commands.slash_command(name="loop", description="Loop the current song or stop the loop")
    @discord.option(
        "max_times",
        description="Maximum number of times this song will be looped; infinite or 0 if omitted (depends on state)",
        required=False,
        input_type=int,
        min_value=1,
    )
    @commands.guild_only()
    async def loop(self, ctx: discord.ApplicationContext, max_times: int) -> None:
        """Set or clear looping for the current song in the guild."""
        if ctx.guild_id is None:
            raise commands.NoPrivateMessage

        if not self.bot.is_active(ctx):
            await ctx.respond("There is nothing to loop.", ephemeral=True)
            return

        loops = self.bot.per_guild_loop_settings.get(ctx.guild_id, 0)
        if loops == 0:
            self.bot.per_guild_loop_settings[ctx.guild_id] = max_times or -1
            await ctx.respond(
                f"The song that is currently played will be looped "
                f"{f'{max_times} time{"s" if max_times > 1 else ""}' if max_times else 'infinitely'}."
            )
        else:
            self.bot.per_guild_loop_settings[ctx.guild_id] = max_times or 0
            await ctx.respond(
                f"Song will be looped {max_times} more time{'s' if max_times > 1 else ''}."
                if max_times
                else "Disabled looping for this song."
            )

    @commands.slash_command(name="info", description="Infos about the current song")
    @commands.guild_only()
    async def info(self, ctx: discord.ApplicationContext) -> None:
        """Display information about the currently playing song."""
        if not self.bot.is_active(ctx):
            await ctx.respond("There is currently no song playing.")
            return

        response = self.current_song_info(ctx)
        if response == "":
            await ctx.respond("Error while retrieving song info. Please try again.", ephemeral=True)
        else:
            await ctx.respond(response)

    @commands.slash_command(name="queue", description="Details about the currently playing song and the queue")
    @commands.guild_only()
    async def queue(self, ctx: discord.ApplicationContext) -> None:
        """Display the current song and upcoming songs in the queue."""
        if ctx.guild_id is None:
            raise commands.NoPrivateMessage

        if not self.bot.is_active(ctx):
            await ctx.respond("There are currently no songs in queue.")
            return

        cutoff = 5

        response = self.current_song_info(ctx) + "\n"

        for i in range(1, len(self.bot.per_guild_song_queues[ctx.guild_id])):
            song = self.bot.per_guild_song_queues[ctx.guild_id][i]

            if i == cutoff:
                response += f"- ...{len(self.bot.per_guild_song_queues[ctx.guild_id]) - cutoff} more song(s).\n"
                break

            duration_string = song["duration_string"]
            if song["duration"] < ONE_MINUTE:
                duration_string = "0:" + duration_string.zfill(2)
            if song["duration"] < ONE_HOUR:
                placeholder = "0:00"
            else:
                placeholder = "0:00:00"

            response += f"- [{song['title']}](<{song['song_link']}>) - ({placeholder} / {duration_string})\n"

        if self.bot.per_guild_is_downloading.get(ctx.guild_id, False):
            response += "\n..._more songs are currently being downloaded_..."

        await ctx.respond(response)

    @commands.slash_command(name="clear_queue", description="Stop playback and clear entire queue")
    @commands.guild_only()
    async def clear_queue(self, ctx: discord.ApplicationContext) -> None:
        """Stop playback and clear the entire song queue for the guild."""
        if ctx.guild_id is None:
            raise commands.NoPrivateMessage

        if not self.bot.is_active(ctx):
            await ctx.respond("Queue is already empty.", ephemeral=True)
            return

        self.bot.cleanup(ctx.guild_id)

        if not ctx.voice_client:
            self.bot.logger.error("Error when clearing queue, no voice_client was found.")
            await ctx.respond("Something went wrong.")
            return
        ctx.voice_client.stop()

        await ctx.respond("Stopped playback and cleared the queue.")

    @commands.slash_command(name="skip", description="Skip the current song")
    @commands.guild_only()
    async def skip(self, ctx: discord.ApplicationContext) -> None:
        """Skip the current song in the guild's queue."""
        if ctx.guild_id is None:
            raise commands.NoPrivateMessage

        if self.bot.is_active(ctx):
            self.bot.per_guild_loop_settings[ctx.guild_id] = 0

            if not ctx.voice_client:
                self.bot.logger.error("Error when skipping song, no voice_client was found.")
                await ctx.respond("Something went wrong.")
                return
            ctx.voice_client.stop()
            await ctx.respond("Song skipped.")
        else:
            await ctx.respond("No audio is currently playing.", ephemeral=True)

    @commands.slash_command(name="pause", description="Pause the current playback")
    @commands.guild_only()
    async def pause(self, ctx: discord.ApplicationContext) -> None:
        """Pause the current song playback."""
        if ctx.guild_id is None:
            raise commands.NoPrivateMessage

        if ctx.voice_client and ctx.voice_client.is_playing():
            ctx.voice_client.pause()
            now = datetime.now()
            self.bot.per_guild_song_queues[ctx.guild_id][0]["passed_time_until_pause"] = (
                now - self.bot.per_guild_song_queues[ctx.guild_id][0].get("starting_time", now)
            )
            await ctx.respond("Playback paused.")
        else:
            await ctx.respond("No audio is currently playing.", ephemeral=True)


def setup(bot: Bot) -> None:
    """Register the `Songs` cog with the bot."""
    bot.add_cog(Songs(bot))
