import asyncio
from datetime import datetime, timedelta
import json
import logging
import os
import re
import sys
from typing import NotRequired, TypedDict, Union, cast

import discord
from discord.channel import VocalGuildChannel
from discord.ext import commands
from discord import option
import yt_dlp

import config
from observable_set import ObservableSet

class YTDLPLogger:
    def __init__(self, guild_id: int):
        self.logger = logging.getLogger()
        self.guild_id = guild_id
        self.download_message_interval = 15

    def debug(self, msg: str):
        if "has already been recorded in" in msg:
            _all_guild_download_ids.setdefault(self.guild_id, []).append(msg.split(':')[0].removeprefix("[download] ")[len("[0;32m"):-len("[0m")])
        if "ETA" in msg:
            if self.download_message_interval == 15:
                self.logger.info(msg.strip())
            elif self.download_message_interval == 0:
                self.download_message_interval = 16
            self.download_message_interval -= 1
        else:
            self.logger.info(msg.strip())
    
    def info(self, msg):
        self.logger.info(msg.strip())
    
    def warning(self, msg):
        self.logger.warning(msg.strip())
    
    def error(self, msg):
        self.logger.error(msg.strip())
    
    def critical(self, msg):
        self.logger.critical(msg.strip())

logging.basicConfig(level=logging.INFO, format='[%(asctime)s] [%(levelname)s]: %(message)s', handlers=[
    logging.FileHandler('babshoven.log'),
    logging.StreamHandler()
])

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

bot = commands.Bot(owner_id=191530044491956224)

##################################################################
############################ GENERAL #############################
##################################################################

DISCONNECTION_COUNTDOWN: int = 300  # seconds until disconnect while inactive and lonely

_all_guild_current_voice_channel_ids: dict[int, int] = {}


def create_embed(title=None, description=None, color=None, footer=None):
    embed_var = discord.Embed(title=title, description=description, color=color)
    embed_var.set_footer(text=footer)
    return embed_var


# Is called when the bot is asked to leave/clear its storage/refresh its state. Clears song queue, resets loop parameter, etc.
def cleanup(guild_id: int):
    guild_queue = _all_guild_song_queues.get(guild_id, []).copy()
    for song in guild_queue:
        try:
            _all_guild_song_queues.get(guild_id, []).remove(song)
        except ValueError:
            pass
        
        remove_downloaded_song(song)
    _all_guild_song_queues.pop(guild_id, None)
    _all_guild_loop_settings.pop(guild_id, None)


def find_dict_by_id(to_search_in: list[Song], id: str):
    filtered_list = [d for d in to_search_in if bool(d)]  # if there are empty dicts in list (error handling purposes), filter those out
    return [d for d in filtered_list if d["id"] == id]


async def disconnect_countdown(channel: VocalGuildChannel):
    countdown = DISCONNECTION_COUNTDOWN // 10
    while (len(channel.members) == 1 and countdown > 0):
        countdown -= 1
        await asyncio.sleep(10)

    if countdown == 0:
        vcs = list(filter(lambda vc: channel.guild.id == cast(discord.Guild, vc.guild).id, cast(list[discord.VoiceClient], bot.voice_clients)))
        if len(vcs) == 0:
            logging.info("I tried to leave, but I already was disconnected earlier.")
            return
        logging.info("Left the voice channel after feeling lonely.")
        vc: discord.VoiceClient = vcs[0]
        cleanup(channel.guild.id)
        if vc.is_playing() or vc.is_paused():
            vc.stop()
        await vc.disconnect()

##################################################################

@bot.slash_command(
    name="ping",
    description="Check the bot's latency"
)
async def ping(ctx: discord.ApplicationContext):
    await ctx.respond(f"Latency: {round(bot.latency * 1000)} ms")

@bot.slash_command(
    name="restart",
    description="Restart the bot (owner only)"
)
@commands.is_owner()
async def restart (ctx: discord.ApplicationContext):
    interaction = await ctx.respond("Restarting...")
    response = await cast(discord.Interaction, interaction).original_response()
    os.execv(sys.executable, ['python'] + sys.argv + [str(response.channel.id), str(response.id)])

@bot.slash_command(
    name="clear_cache",
    description="Clear download cache (owner only)"
)
@commands.is_owner()
async def clear_cache(ctx: discord.ApplicationContext):
    _download_archive.clear()
    await ctx.respond("Cleared download cache.")

@bot.slash_command(
    name="override_limits",
    description="Override the bot's limits (owner only)"
)
@option(
    "max_song_length", 
    description="Maximum song length in minutes",
    required=False,
    input_type=int
)
@option(
    "playlist_limit", 
    description="Maximum number of songs in a playlist",
    required=False,
    input_type=int
)
@commands.is_owner()
async def override_limits(ctx: discord.ApplicationContext, max_song_length: int, playlist_limit: int):
    if not (max_song_length or playlist_limit):
        await ctx.respond("You need to specify at least one option.", ephemeral=True)
        return
    
    await ctx.defer()
    
    global SONG_MAX_LENGTH_MINUTES, PLAYLIST_SONGS_LIMIT
    if max_song_length:
        await ctx.respond(f"Changed maximum song duration from {SONG_MAX_LENGTH_MINUTES} to {max_song_length}!")
        SONG_MAX_LENGTH_MINUTES = max_song_length
    if playlist_limit:
        await ctx.respond(f"Changed maximum number of songs per playlist from {PLAYLIST_SONGS_LIMIT} to {playlist_limit}!")
        PLAYLIST_SONGS_LIMIT = playlist_limit
    
    for option in cast(discord.SlashCommand, play).options:
        if option.name == 'playlist_limit':
            option.description = option.description.rsplit(' ', 1)[0] + " " + str(PLAYLIST_SONGS_LIMIT)
            option.max_value = PLAYLIST_SONGS_LIMIT
            break
    
    await bot.sync_commands()

@bot.event
async def on_application_command_error(ctx: discord.ApplicationContext, error: discord.DiscordException):
    if isinstance(error, commands.NotOwner):
        await ctx.respond("Sorry, only the bot owner can use this command!", ephemeral=True)
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
_all_guild_download_ids: dict[int, list[str]] = {}  # contains ids of all songs that were tried to be downloaded, but denied due to already being present in download_archive (refreshed after every play command)
_download_archive: ObservableSet = ObservableSet(logger=logging.getLogger())
_all_guild_active_download_markers: dict[int, bool] = {}
_all_guild_song_queues: dict[int, list[Song]] = {}
_all_guild_added_songs: dict[int, Song] = {}
_all_guild_loop_settings: dict[int, int] = {}  # (guild_id: -n | 0 | +n) -> -n: loop infinite, otherwise +n times
_stop_downloading_interaction: discord.Interaction | discord.WebhookMessage | None = None


def is_active(ctx: discord.ApplicationContext):
    return ctx.voice_client and (ctx.voice_client.is_playing() or ctx.voice_client.is_paused())


def current_song_info(ctx: discord.ApplicationContext):
    response = ""
    try:
        current_song = _all_guild_song_queues[ctx.guild_id][0]
    except (KeyError, IndexError):
        return ""
    loops = _all_guild_loop_settings.get(ctx.guild_id, 0)
    
    if not current_song or not ctx.voice_client:
        return ""

    if ctx.voice_client.is_playing():
        now = datetime.now()
        runtime = str(now - current_song.get("starting_time", now)).split('.')[0]
    elif ctx.voice_client.is_paused():
        response += "# [Playback is paused]\n"
        runtime = str(current_song.get("passed_time_until_pause", timedelta(0))).split('.')[0]
    else:
        runtime = "0:00:00"
    
    duration_string = current_song["duration_string"]
    if current_song["duration"] < 60:  # if song is under 1 minute, duration_string is just the number of seconds
        duration_string = "0:" + duration_string.zfill(2)
    if current_song["duration"] < 60 * 60:  # song is shorter than 1 hour, make 0:01:23 -> 1:23
        runtime = runtime.removeprefix("0:").removeprefix("0")
        
    response += f"- **[{current_song["title"]}](<{current_song["song_link"]}>) - ({runtime} / {duration_string})"
    
    if loops != 0:
        response += f" [Looped: {loops if loops > 0 else '\u221e'} time{'s' if loops != 1 else ''} left]"
    
    return response + "**"
    

# Delete the last played song if it's not in any song queue anymore; current_song must have been removed from the current guild queue beforehand.
def remove_downloaded_song(current_song: Song | None):
    if not current_song:
        return

    # check if any of the song queues contains the filename
    filename = current_song['filename']
    all_songs = _all_guild_song_queues.values()
    if not any(song['filename'] == filename for queue in all_songs for song in queue):
        try:
            os.remove(filename)
            logging.info(f"Deleted {filename} successfully.")
            _download_archive.discard(current_song["archive_id"])
        except FileNotFoundError:
            logging.error(f"Deleting {filename} failed, file was not found.")
            pass

# Function is called every time a song finishes to be able to start the next one from the queue.
async def play_next(ctx: discord.ApplicationContext):
    guild_id = ctx.guild_id
    volume = _all_guild_volume_settings.get(guild_id, DEFAULT_BOT_VOLUME)    
    loops = _all_guild_loop_settings.get(guild_id, 0)

    if loops == 0:
        if len(_all_guild_song_queues[guild_id]) == 0:
            return
    else:
         _all_guild_loop_settings[guild_id] -= 1

    passed_time = _all_guild_song_queues[guild_id][0].get("passed_time", timedelta(seconds=0))
    _all_guild_song_queues[guild_id][0]["starting_time"] = datetime.now() - passed_time

    source = await discord.FFmpegOpusAudio.from_probe(
        _all_guild_song_queues[guild_id][0]['filename'], method='fallback',
            before_options=f"-ss {str(passed_time)}", options=f"-af 'volume={volume}'")
    
    _all_guild_song_queues[guild_id][0]["passed_time"] = timedelta(seconds=0)  # reset passed_time in case of loops
    
    def song_has_ended(e):
        loops = _all_guild_loop_settings.get(guild_id, 0)
        # try to remove song only if it's not actively being looped
        if loops == 0:
            try:
                remove_downloaded_song(_all_guild_song_queues.get(guild_id, [None]).pop(0))
            except IndexError:
                pass
        
        return asyncio.run_coroutine_threadsafe(play_next(ctx), bot.loop)
    
    if not ctx.voice_client:
        logging.error("Error while trying to start playback, no voice_client was found.")
        cleanup(guild_id)
        return
    
    try:
        ctx.voice_client.play(source, after=song_has_ended)
    except discord.errors.ClientException as e:
        logging.error(f"Error while trying to start playback: {e}")
        cleanup(guild_id)
        if is_active(ctx):
            ctx.voice_client.stop()
        return
    
    if _pause_after_play.get(guild_id, False):
        ctx.voice_client.pause()
        _pause_after_play[guild_id] = False

##################################################################

@bot.slash_command(
    name="leave",
    description="Leave the voice channel"
)
@commands.guild_only()
async def leave(ctx: discord.ApplicationContext):
    if ctx.voice_client and ctx.voice_client.is_connected():
        await ctx.voice_client.disconnect()
        await ctx.respond("Left the voice channel.")
    else:
        await ctx.respond("I am not in a voice channel!", ephemeral=True)

@bot.slash_command(
    name="volume",
    description="Adjust the volume"
)
@option(
    "value",
    description=f"Enter a value between 1 and 100, default is {int(DEFAULT_BOT_VOLUME * 100)}",
    required=False,
    input_type=int,
    min_value=1,
    max_value=100
)
@commands.guild_only()
async def volume(ctx: discord.ApplicationContext, value: int):
    current_volume = int((_all_guild_volume_settings.get(ctx.guild_id, DEFAULT_BOT_VOLUME)) * 100)
    
    if not value or value == current_volume:
        await ctx.respond(f"Volume currently is set to {current_volume}%.")
        return
    
    _all_guild_volume_settings[ctx.guild_id] = float(value) / 100
    
    try:
        with open(VOLUME_SETTINGS_FILE_PATH, 'w') as file:
            json.dump(_all_guild_volume_settings, file, indent=4)
    except (OSError, json.JSONDecodeError) as e:
        _all_guild_volume_settings[ctx.guild_id] = DEFAULT_BOT_VOLUME
        logging.error(f"Storing new volume setting for guild '{ctx.guild}' failed: {e}")
        await ctx.respond("Changing the volume failed, please try again.")
        return
    
    # apply volume to playing songs
    if ctx.voice_client:
        try:
            if ctx.voice_client.is_playing():
                now = datetime.now()
                passed_time = now - _all_guild_song_queues[ctx.guild_id][0].get("starting_time", now)
                _all_guild_song_queues[ctx.guild_id][0]["passed_time"] = passed_time
            elif ctx.voice_client.is_paused():
                _all_guild_song_queues[ctx.guild_id][0]["passed_time"] = _all_guild_song_queues[ctx.guild_id][0].get("passed_time_until_pause", timedelta(0))
                _pause_after_play[ctx.guild_id] = True
        except KeyError as e:
            ctx.respond("Couldn't apply new volume to current song. New volume will be applied to the next song in queue.")
            logging.error(f"Failed to apply volume to current song: {e}")
            return
        
        if is_active(ctx):
            loops = _all_guild_loop_settings.get(ctx.guild_id, 0)
            _all_guild_loop_settings[ctx.guild_id] = loops + 1 if loops >= 0 else loops
            ctx.voice_client.stop()
    
    await ctx.respond(f"Changed the volume to {value}%.")

@bot.slash_command(
    name="stop_download",
    description="Stop downloading the playlist (does not stop the current song being downloaded)"
)
@commands.guild_only()
async def stop_downloading(ctx: discord.ApplicationContext):
    if not _all_guild_active_download_markers.get(ctx.guild_id, False):
        await ctx.respond("No songs are being downloaded right now.", ephemeral=True)
        return
    
    global _stop_downloading_interaction
    _stop_downloading_interaction = await ctx.respond(f"Trying to stop the download of remaining songs  <a:loading:1373455971296346153>")
    
    counter = 0
    while counter < 5:
        await asyncio.sleep(1)
        if not _stop_downloading_interaction:
            break
        counter += 1
    
    # if the download hasn't stopped after 5s, the download probably finished too soon
    if counter == 5:
        await ctx.edit(content="Couldn't stop the download.")
    

@bot.slash_command(
    name="play",
    description="Add a YouTube video to the queue or resume paused playback (if all parameters are left empty)"
)
@option(
    "url", 
    description="Link to the YouTube video",
    required=False
)
@option(
    "search_terms", 
    description="Search for a YouTube video",
    required=False
)
@option(
    "playlist_limit", 
    description=f"Don't load more than <...> songs for this playlist, default is {PLAYLIST_SONGS_LIMIT}",
    required=False,
    input_type=int,
    min_value=1,
    max_value=PLAYLIST_SONGS_LIMIT
)
@commands.guild_only()
async def play(ctx: discord.ApplicationContext, url: str, search_terms: str, playlist_limit: int):
    playlist_limit = playlist_limit or PLAYLIST_SONGS_LIMIT
    guild_id: int = ctx.guild_id
    counter_for_added_songs = 0
    responded = False  # set to true for ctx.respond's that do not return immediately after
    silent_mode = False  # whether to respond with updates, is turned on when re-downloading songs in a playlist
    
    _all_guild_added_songs[guild_id] = {'archive_id': "",
                                        'id': "",
                                        'filename': "",
                                        'title': "",
                                        'song_link': "",
                                        'duration_string': "",
                                        'duration': 0}
    
    def add_archive_id(element: str):
        _all_guild_added_songs[guild_id]["archive_id"] = element
    
    _all_guild_song_queues.setdefault(guild_id, [])
    _all_guild_volume_settings.setdefault(guild_id, DEFAULT_BOT_VOLUME)
    _download_archive.set_callback(add_archive_id, overwrite=False)
    
    if url and search_terms:
        await ctx.respond("Don't use both parameters at the same time.", ephemeral=True)
        return

    if cast(discord.Member, ctx.author).voice:
        channel = cast(discord.VoiceState, cast(discord.Member, ctx.author).voice).channel
        if ctx.voice_client and ctx.voice_client.is_connected():
            if channel and channel != ctx.voice_client.channel:
                if url or search_terms or is_active(ctx):
                    await ctx.voice_client.move_to(channel)
                    if not (url or search_terms):
                        await ctx.respond("Continuing playback in your new voice channel!")
                        return
        else:
            if url or search_terms:
                try:
                    await cast(Union[discord.VoiceChannel, discord.StageChannel], channel).connect(timeout=2)
                except asyncio.TimeoutError as e:
                    logging.error(f"An error occured while connecting to the voice channel: {e}")
                    await ctx.respond("I couldn't join your voice channel. Please check my permissions and try again.")
                    return                    
                
    else:
        await ctx.respond("You are not in a voice channel!", ephemeral=True)
        return
    
    if not url and not search_terms:
        if ctx.voice_client and ctx.voice_client.is_paused():
            ctx.voice_client.resume()
            _all_guild_song_queues[guild_id][0]["starting_time"] = datetime.now() - _all_guild_song_queues[guild_id][0].get("passed_time_until_pause", timedelta(0))
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
    followup_message = None
    
    async def download_reporter():
        nonlocal downloading_started, followup_message
        message = followup_message or ctx
        
        if isinstance(followup_message, discord.WebhookMessage):
            await cast(discord.ApplicationContext, message).edit(content="Started downloading the next song!")
        elif followup_message:
            followup_message = await ctx.send_followup("Started downloading the next song!", wait=True)
            message = followup_message
        else:
            await cast(discord.ApplicationContext, message).edit(content="Started downloading!")
        
        while True:
            await asyncio.sleep(1)
            if download_dict['status'] == 'finished':
                break
            
            total_bytes = download_dict.get('total_bytes', download_dict.get('total_bytes_estimate', 1))
            progress = f"{(download_dict.get('downloaded_bytes', 0) / total_bytes):.0%}" if total_bytes > 1 else "Unknown"
            eta = download_dict.get('eta', 0)
            if eta > 0 and not cast(float, eta).is_integer():
                eta = str(timedelta(seconds=eta))[:-3]
            else:
                eta = str(timedelta(seconds=eta))
                
            await cast(discord.ApplicationContext, message).edit(content=f"_Downloading song_  <a:loading:1373455971296346153>\n" +
                                        f"- **Progress:** {progress}\n- **Time left (estimate):** {eta}" +
                                        f"\n- **Elapsed time:** {str(timedelta(seconds=download_dict['elapsed'])).split('.')[0]}")
            await asyncio.sleep(1)
        
        downloading_started = False

    async def processing_reporter():
        nonlocal processing_started, followup_message
        
        sleep_duration = 0
        while followup_message and not isinstance(followup_message, discord.WebhookMessage):
            await asyncio.sleep(0.1)
            sleep_duration += 0.1
            if sleep_duration >= 5:  # safeguard, don't wait too long in case of bugs/errors
                logging.error("followup_message was never assigned properly. No longer wait for it to change.")
                return
        
        message = followup_message or ctx
        
        await message.edit(content=f"Download has finished, finalizing  <a:loading:1373455971296346153>")
        
        while True:
            await asyncio.sleep(1)
            if processing_dict['status'] == 'finished' and processing_dict['postprocessor'] == 'MoveFiles':
                break
        processing_started = False
        
        if followup_message:  # Don't print song info if we're at index >= 2 of playlist
            return
        followup_message = True  # if we're calling the download_reporter again, the followup_message should be active
        
        sleep_duration = 0
        while not _all_guild_added_songs.get(guild_id):
            await asyncio.sleep(0.1)
            sleep_duration += 0.1
            if sleep_duration >= 5:  # safeguard, don't wait too long in case of bugs/errors
                logging.error("added_song wasn't populated in time. No longer wait for it to change.")
                await ctx.edit(content="Error upon adding your song(s) to the queue. Please try again.")
                return
        
        queue_length = len(_all_guild_song_queues.get(guild_id, []))
        if queue_length == 1:
            await ctx.edit(content=f"Queue is empty, [{_all_guild_added_songs[guild_id]['title']}]({_all_guild_added_songs[guild_id]["song_link"]}) started to play.")
        elif queue_length == 0:
            await ctx.edit(content="Error upon adding your song(s) to the queue. Please try again.")
        else:
            await ctx.edit(content=f"[{_all_guild_added_songs[guild_id]['title']}]({_all_guild_added_songs[guild_id]["song_link"]}) was added to the queue " +
                                    f"at position **{len(_all_guild_song_queues[guild_id])}**.")
    
    def download_hooks(d):
        if silent_mode:
            return
        nonlocal downloading_started, download_dict, responded
        download_dict = d
        if d['status'] == 'downloading':
            if not downloading_started:
                bot.loop.create_task(download_reporter())
                responded = True
                downloading_started = True
    
    ydl = None
    def processing_hooks(d):
        nonlocal processing_started, processing_dict, ydl, counter_for_added_songs
        processing_dict = d
        if d['status'] == 'started':
            if not processing_started and not silent_mode:
                bot.loop.create_task(processing_reporter())
                processing_started = True
        if d['status'] == 'finished' and d['postprocessor'] == 'MoveFiles':
            info_dict = d['info_dict']
            
            if not info_dict:  # should never happen, but you can't be too careful
                return
            
            filename = cast(str, cast(yt_dlp.YoutubeDL, ydl).prepare_filename(info_dict))
            mp3_filename = filename.rsplit('.', 1)[0] + '.mp3'

            if not os.path.isfile(mp3_filename):
                os.rename(filename, mp3_filename)

            _all_guild_added_songs[guild_id] = {'archive_id': "",
                                                'id': cast(str, info_dict.get('id', "")),
                                                'filename': mp3_filename,
                                                'title': cast(str, info_dict.get('title', "")),
                                                'song_link': cast(str, info_dict.get('webpage_url', "")),
                                                'duration_string': cast(str, info_dict.get('duration_string', "")),
                                                'duration': cast(int, info_dict.get('duration', 0))}
            
            _all_guild_song_queues[guild_id].append(_all_guild_added_songs[guild_id])
            counter_for_added_songs += 1
            if not is_active(ctx):
                bot.loop.create_task(play_next(ctx))
                
    
    def download_control(info, *, incomplete):
        global _stop_downloading_interaction
        duration = info.get('duration')
        if (duration and duration > SONG_MAX_LENGTH_MINUTES * 60):
            return f"'{info.get('title')}' is too long"
        if _stop_downloading_interaction:
            raise yt_dlp.utils.DownloadCancelled("Stop the downloads!")

    ydl_opts = {
        'download_archive': _download_archive,
        'format': 'bestaudio/best',
        'ignoreerrors': True,
        'logger': YTDLPLogger(guild_id),
        'match_filter': download_control,
        'noplaylist': True if search_terms else False,
        'paths': {'home': 'downloads/'},
        'playlist_items': str(list(range(playlist_limit + 1))).replace(' ', '')[1:-1],
        'postprocessor_hooks': [processing_hooks],
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '192',
        }],
        'progress_hooks': [download_hooks],
        #'verbose': True,
        #'ratelimit': 250000,
    }
    
    def download_songs(_url = url):
        nonlocal ydl
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            return ydl.extract_info(_url or f"ytsearch:{search_terms}")
    
    info_dict = None
    
    global _stop_downloading_interaction
    was_cancelled = False
    try:
        _all_guild_active_download_markers[guild_id] = True
        info_dict = await asyncio.to_thread(download_songs)
    except yt_dlp.utils.DownloadCancelled:
        message = _stop_downloading_interaction
        _stop_downloading_interaction = None
        await cast(Union[discord.Interaction, discord.WebhookMessage], message).edit(content="Stopped downloading the remaining song(s)!")
        was_cancelled = True
    finally:
        _all_guild_active_download_markers[guild_id] = False
    
    already_downloaded = _all_guild_download_ids.setdefault(guild_id, [])
    all_songs = _all_guild_song_queues[guild_id]
    add_to_queue = []
    for id in already_downloaded:
        try:
            add_to_queue.append(find_dict_by_id(all_songs, id)[0])
        except IndexError:
            logging.error(f'Tried to add {id} to the queue, but couldn\'t find it in the list.')
            pass
    
    del _all_guild_download_ids[guild_id]

    was_error = True
    for song in add_to_queue:
        if song.get("archive_id") in _download_archive:  # song is still present in the downloads
            _all_guild_song_queues[guild_id].append(song)
            counter_for_added_songs += 1
            was_error = False
        else:  # song is not downloaded anymore by the time execution arrived here, re-download it
            try:
                silent_mode = True
                await asyncio.to_thread(download_songs, f"https://www.youtube.com/watch?v={song['id']}")
                was_error = False
            except yt_dlp.utils.DownloadError as e:
                logging.error(f"Download of song failed: {e}")
                continue
    
    if was_cancelled or (info_dict and (playlist_count := info_dict.get('playlist_count', 0)) > 1):
        response = (f"Finished downloading the playlist. {counter_for_added_songs}{"" if was_cancelled else (f" / {playlist_count}")} " + # type: ignore
                    "songs were added to the queue.")
        if not was_cancelled and counter_for_added_songs < playlist_count and counter_for_added_songs < playlist_limit: # type: ignore
            response += f"\n\nAn error occurred. Make sure that no song is longer than **{SONG_MAX_LENGTH_MINUTES} minutes or age-restricted**, and try again."
        if isinstance(followup_message, discord.WebhookMessage):  # responded to its initial message earlier in the download process, edit the response
            await cast(discord.WebhookMessage, followup_message).edit(content=response)
        else:
            await ctx.respond(response)
        return
    
    if not responded:
        if not was_error and len(add_to_queue) > 0:
            queue_length = len(_all_guild_song_queues[guild_id])
            if queue_length == 1:
                await ctx.edit(content=f"Queue is empty, [{add_to_queue[0]['title']}]({add_to_queue[0]["song_link"]}) started to play.")
            else:
                await ctx.respond(f"[{add_to_queue[0]['title']}]({add_to_queue[0]["song_link"]}) was added to the queue at position **{queue_length}**.")
            return
        await ctx.respond("There were errors downloading your song(s). Please try again, and make sure that no song is longer " +
                                    f"than **{SONG_MAX_LENGTH_MINUTES} minutes or age-restricted**.")


@bot.slash_command(
    name="loop",
    description="Loop the current song or stop the loop"
)
@option(
    "max_times",
    description="Maximum number of times this song will be looped; infinite or 0 if omitted (depends on state)",
    required=False,
    input_type=int,
    min_value=1
)
@commands.guild_only()
async def loop(ctx: discord.ApplicationContext, max_times: int):
    if not is_active(ctx):
        await ctx.respond("There is nothing to loop.", ephemeral=True)
        return
    
    loops = _all_guild_loop_settings.get(ctx.guild_id, 0)
    if loops == 0:
        _all_guild_loop_settings[ctx.guild_id] = max_times or -1
        await ctx.respond(f"The song that is currently played will be looped {f"{max_times} time{'s' if max_times > 1 else ''}" if max_times else "infinitely"}.")
    else:
        _all_guild_loop_settings[ctx.guild_id] = max_times or 0
        await ctx.respond(f"Song will be looped {max_times} more time{'s' if max_times > 1 else ''}." if max_times else "Disabled looping for this song.")
        

@bot.slash_command(
    name="info",
    description="Infos about the current song"
)
@commands.guild_only()
async def info(ctx: discord.ApplicationContext):
    if not is_active(ctx):
        await ctx.respond("There is currently no song playing.")
        return
    
    response = current_song_info(ctx)
    if response == "":
        await ctx.respond("Error while retrieving song info. Please try again.", ephemeral=True)
    else:
        await ctx.respond(response)


@bot.slash_command(
    name="queue",
    description="Details about the currently playing song and the queue"
)
@commands.guild_only()
async def queue(ctx: discord.ApplicationContext):
    if not is_active(ctx):
        await ctx.respond("There are currently no songs in queue.")
        return
    
    cutoff = 5
    
    response = current_song_info(ctx) + '\n'

    for i in range(1, len(_all_guild_song_queues[ctx.guild_id])):
        song = _all_guild_song_queues[ctx.guild_id][i]

        if i == cutoff:
            response += f"- ...{len(_all_guild_song_queues[ctx.guild_id]) - cutoff} more song(s).\n"
            break

        duration_string = song["duration_string"]
        if song["duration"] < 60:  # if song is under 1 minute, duration_string is just the number of seconds
            duration_string = "0:" + duration_string.zfill(2)
        if song["duration"] < 60 * 60:  # song is shorter than 1 hour
            placeholder = "0:00"
        else:
            placeholder = "0:00:00"
            
        response += f"- [{song["title"]}](<{song["song_link"]}>) - ({placeholder} / {duration_string})\n"
        
    if _all_guild_active_download_markers.get(ctx.guild_id, False):
        response += "\n..._more songs are currently being downloaded_..."
    
    await ctx.respond(response)

@bot.slash_command(
    name="clear_queue",
    description="Stop playback and clear entire queue"
)
@commands.guild_only()
async def clear_queue(ctx: discord.ApplicationContext):
    if not is_active(ctx):
        await ctx.respond("Queue is already empty.", ephemeral=True)
        return
    
    cleanup(ctx.guild_id)
    
    if not ctx.voice_client:
        logging.error("Error when clearing queue, no voice_client was found.")
        await ctx.respond("Something went wrong.")
        return
    ctx.voice_client.stop()
    
    await ctx.respond("Stopped playback and cleared the queue.")

@bot.slash_command(
    name="skip",
    description="Skip the current song"
)
@commands.guild_only()
async def skip(ctx: discord.ApplicationContext):
    if is_active(ctx):
        _all_guild_loop_settings[ctx.guild_id] = 0
        
        if not ctx.voice_client:
            logging.error("Error when skipping song, no voice_client was found.")
            await ctx.respond("Something went wrong.")
            return
        ctx.voice_client.stop()
        await ctx.respond("Song skipped.")
    else:
        await ctx.respond("No audio is currently playing.", ephemeral=True)

@bot.slash_command(
    name="pause",
    description="Pause the current playback"
)
@commands.guild_only()
async def pause(ctx: discord.ApplicationContext):
    if ctx.voice_client and ctx.voice_client.is_playing():
        ctx.voice_client.pause()
        now = datetime.now()
        _all_guild_song_queues[ctx.guild_id][0]["passed_time_until_pause"] = now - _all_guild_song_queues[ctx.guild_id][0].get("starting_time", now)
        await ctx.respond("Playback paused.")
    else:
        await ctx.respond("No audio is currently playing.", ephemeral=True)

##################################################################
############################ RUN BOT #############################
##################################################################

@bot.listen # type: ignore
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    if after.channel:
        if member == bot.user:
            _all_guild_current_voice_channel_ids[after.channel.guild.id] = after.channel.id
            return
    
    if not after.channel and member == bot.user:
        guild_id = cast(Union[discord.VoiceChannel, discord.StageChannel], before.channel).guild.id
        cleanup(guild_id)
        return
    
    if (before.channel and before.channel.id == _all_guild_current_voice_channel_ids.get(before.channel.guild.id, None) and
        len(before.channel.members) == 1 and before.channel.members[0] == bot.user):
        bot.loop.create_task(disconnect_countdown(before.channel))

@bot.listen(once=True)
async def on_ready():
    global _all_guild_volume_settings
    # initialize json
    try:
        with open(VOLUME_SETTINGS_FILE_PATH, 'r') as file:
            _all_guild_volume_settings = json.load(file, object_pairs_hook=lambda pairs: {int(k): v for k,v in pairs})
    except (OSError, json.JSONDecodeError) as e:
        logging.error(f"Error upon reading {VOLUME_SETTINGS_FILE_PATH}: {e}")
        pass
    
    logging.info(f'Logged in as {bot.user}')
    
    # Called after bot was restarted via command
    if (len(sys.argv) > 2):
        channel = bot.get_channel(int(sys.argv[1]))
        msg = await cast(discord.TextChannel, channel).fetch_message(int(sys.argv[2]))
        await msg.edit(content="Restart has finished, I'm back!")


bot.run(config.DISCORD_TOKEN)
