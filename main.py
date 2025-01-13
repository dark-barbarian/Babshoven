import asyncio
from datetime import datetime, timedelta
import json
import logging
import os
import re

import discord
from discord.channel import VocalGuildChannel
from discord.ext import commands
from discord import option
import yt_dlp

import config
from observable_set import ObservableSet

class YTDLPLogger:
    def __init__(self, guild_id: str):
        self.logger = logging.getLogger()
        self.guild_id = guild_id

    def debug(self, msg: str):
        if "has already been recorded in" in msg:
            ALL_GUILD_DOWNLOAD_IDS.setdefault(self.guild_id, []).append(msg.split(':')[0].removeprefix("[download] ")[len("[0;32m"):-len("[0m")])
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

bot = commands.Bot(owner_id=191530044491956224)

##################################################################
############################ GENERAL #############################
##################################################################

ALL_GUILD_CURRENT_VOICE_CHANNEL_IDS: dict[int, int] = {}

DISCONNECTION_COUNTDOWN: int = 300  # seconds until disconnect while inactive and lonely

def create_embed(title=None, description=None, color=None, footer=None):
    embed_var = discord.Embed(title=title, description=description, color=color)
    embed_var.set_footer(text=footer)
    return embed_var


# Is called when the bot is asked to leave/clear its storage/refresh its state. Clears song queue, resets loop parameter, etc.
def cleanup(guild_id: int):
    guild_queue = ALL_GUILD_SONG_QUEUES.get(guild_id, []).copy()
    for song in guild_queue:
        try:
            ALL_GUILD_SONG_QUEUES.get(guild_id, []).remove(song)
        except ValueError:
            pass
        
        remove_downloaded_song(song)
    ALL_GUILD_SONG_QUEUES.pop(guild_id, None)
    ALL_GUILD_LOOP_SETTINGS.pop(guild_id, None)


def find_dict_by_id(to_search_in: list[dict[str, str | int | datetime | timedelta]], id: str):
    filtered_list = [d for d in to_search_in if bool(d)]  # if there are empty dicts in list (error handling purposes), filter those out
    return [d for d in filtered_list if d["id"] == id]


async def disconnect_countdown(channel: VocalGuildChannel):
    countdown = DISCONNECTION_COUNTDOWN // 10
    while (len(channel.members) == 1 and countdown > 0):
        countdown -= 1
        await asyncio.sleep(10)

    if countdown == 0:
        vc = list(filter(lambda vc: channel.guild.id == vc.guild.id, bot.voice_clients))
        if len(vc) == 0:
            logging.info("I tried to leave, but I already was disconnected earlier.")
            return
        logging.info("Left the voice channel after feeling lonely.")
        vc: discord.VoiceClient = vc[0]
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
    name="override_limits",
    description="Override the limits of this bot (owner only)"
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
        await ctx.respond("You need to specify at least one option.")
        return
    
    global SONG_MAX_LENGTH_MINUTES, PLAYLIST_SONGS_LIMIT
    if max_song_length:
        await ctx.respond(f"Changed maximum song duration from {SONG_MAX_LENGTH_MINUTES} to {max_song_length}!")
        SONG_MAX_LENGTH_MINUTES = max_song_length
    if playlist_limit:
        await ctx.respond(f"Changed maximum number of songs per playlist from {PLAYLIST_SONGS_LIMIT} to {playlist_limit}!")
        PLAYLIST_SONGS_LIMIT = playlist_limit
    
    #await bot.register_command(play)  # not implemented in pycord, maybe one day

@bot.event
async def on_application_command_error(ctx: discord.ApplicationContext, error: discord.DiscordException):
    if isinstance(error, commands.NotOwner):
        await ctx.respond("Sorry, only the bot owner can use this command!")
    elif isinstance(error, commands.NoPrivateMessage):
        await ctx.respond("Sorry, this command can't be used in a DM!")
    else:
        logging.error(error)
        raise error

##################################################################
######################### MUSIC METHODS ##########################
##################################################################

DEFAULT_BOT_VOLUME = 0.2
ALL_GUILD_VOLUME_SETTINGS: dict[int, float] = {}
VOLUME_SETTINGS_FILE_PATH = "./volumesettings.json"

PAUSE_AFTER_PLAY: dict[int, bool] = {}

ALL_GUILD_DOWNLOAD_IDS: dict[int, list[str]] = {}  # contains ids of all songs that were tried to be downloaded, but denied due to already being present in download_archive (refreshed after every play command)
ALL_GUILD_DOWNLOAD_ARCHIVES: ObservableSet = ObservableSet()
ALL_GUILD_ACTIVE_DOWNLOAD_MARKERS: dict[int, bool] = {}
ALL_GUILD_SONG_QUEUES: dict[int, list[dict[str, str | int | datetime | timedelta]]] = {}
SONG_MAX_LENGTH_MINUTES = 60
PLAYLIST_SONGS_LIMIT = 50
added_song = {}  # necessary to be global for the archive observer in /play

ALL_GUILD_LOOP_SETTINGS: dict[int, int] = {}  # (guild_id: -n | 0 | +n) -> -n: loop infinite, otherwise +n times

_stop_downloading_interaction: discord.Interaction | discord.WebhookMessage = None


def is_active(ctx: discord.ApplicationContext):
    return ctx.voice_client and (ctx.voice_client.is_playing() or ctx.voice_client.is_paused())


def current_song_info(ctx: discord.ApplicationContext):
    response = ""
    try:
        current_song = ALL_GUILD_SONG_QUEUES[ctx.guild_id][0]
    except (KeyError, IndexError):
        return ""
    loops = ALL_GUILD_LOOP_SETTINGS.get(ctx.guild_id, 0)
    
    if not current_song:
        return ""

    if ctx.voice_client.is_playing():
        runtime = str(datetime.now() - current_song["starting_time"]).split('.')[0]
    elif ctx.voice_client.is_paused():
        response += "# [Playback is paused]\n"
        runtime = str(current_song["passed_time_until_pause"]).split('.')[0]
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
def remove_downloaded_song(current_song: dict[str, str | int | datetime | timedelta]):
    if not current_song:
        return

    # check if any of the song queues contains the filename
    filename = current_song['filename']
    all_songs = ALL_GUILD_SONG_QUEUES.values()
    if not any(song['filename'] == filename for queue in all_songs for song in queue):
        try:
            os.remove(filename)
            ALL_GUILD_DOWNLOAD_ARCHIVES.discard(current_song["archive_id"])
        except FileNotFoundError:
            pass

# Function is called every time a song finishes to be able to start the next one from the queue.
async def play_next(ctx: discord.ApplicationContext):
    guild_id = ctx.guild_id
    volume = ALL_GUILD_VOLUME_SETTINGS.get(guild_id, DEFAULT_BOT_VOLUME)    
    loops = ALL_GUILD_LOOP_SETTINGS.get(guild_id, 0)

    if loops == 0:
        if len(ALL_GUILD_SONG_QUEUES[guild_id]) == 0:
            return
    else:
         ALL_GUILD_LOOP_SETTINGS[guild_id] -= 1

    passed_time = ALL_GUILD_SONG_QUEUES[guild_id][0].get("passed_time", timedelta(seconds=0))
    ALL_GUILD_SONG_QUEUES[guild_id][0]["starting_time"] = datetime.now() - passed_time

    source = await discord.FFmpegOpusAudio.from_probe(
        ALL_GUILD_SONG_QUEUES[guild_id][0]['filename'], method='fallback',
            before_options=f"-ss {str(passed_time)}", options=f"-af 'volume={volume}'")
    
    ALL_GUILD_SONG_QUEUES[guild_id][0]["passed_time"] = timedelta(seconds=0)  # reset passed_time in case of loops
    
    def song_has_ended(e):
        loops = ALL_GUILD_LOOP_SETTINGS.get(guild_id, 0)
        # try to remove song only if it's not actively being looped
        if loops == 0:
            try:
                remove_downloaded_song(ALL_GUILD_SONG_QUEUES.get(guild_id, [None]).pop(0))
            except IndexError:
                pass
        
        return asyncio.run_coroutine_threadsafe(play_next(ctx), bot.loop)
    
    try:
        ctx.voice_client.play(source, after=song_has_ended)
    except discord.errors.ClientException as e:
        logging.error(f"Error while trying to start playback: {e}")
        cleanup(guild_id)
        if is_active(ctx):
            ctx.voice_client.stop()
        return
    
    
    if PAUSE_AFTER_PLAY.get(guild_id, False):
        ctx.voice_client.pause()
        PAUSE_AFTER_PLAY[guild_id] = False

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
        await ctx.respond("I am not in a voice channel!")

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
    current_volume = int((ALL_GUILD_VOLUME_SETTINGS.get(ctx.guild_id, DEFAULT_BOT_VOLUME)) * 100)
    
    if not value or value == current_volume:
        await ctx.respond(f"Volume currently is set to {current_volume}%.")
        return
    
    ALL_GUILD_VOLUME_SETTINGS[ctx.guild_id] = float(value) / 100
    
    try:
        with open(VOLUME_SETTINGS_FILE_PATH, 'w') as file:
            json.dump(ALL_GUILD_VOLUME_SETTINGS, file, indent=4)
    except (OSError, json.JSONDecodeError) as e:
        ALL_GUILD_VOLUME_SETTINGS[ctx.guild_id] = DEFAULT_BOT_VOLUME
        logging.error(f"Storing new volume setting for guild '{ctx.guild}' failed: {e}")
        await ctx.respond("Changing the volume failed, please try again.")
        return
    
    # apply volume to playing songs
    if ctx.voice_client:
        if ctx.voice_client.is_playing():
            passed_time = datetime.now() - ALL_GUILD_SONG_QUEUES[ctx.guild_id][0]["starting_time"]
            ALL_GUILD_SONG_QUEUES[ctx.guild_id][0]["passed_time"] = passed_time
        elif ctx.voice_client.is_paused():
            ALL_GUILD_SONG_QUEUES[ctx.guild_id][0]["passed_time"] = ALL_GUILD_SONG_QUEUES[ctx.guild_id][0]["passed_time_until_pause"]
            PAUSE_AFTER_PLAY[ctx.guild_id] = True
        
        if is_active(ctx):
            loops = ALL_GUILD_LOOP_SETTINGS.get(ctx.guild_id, 0)
            ALL_GUILD_LOOP_SETTINGS[ctx.guild_id] = loops + 1 if loops >= 0 else loops
            ctx.voice_client.stop()
    
    await ctx.respond(f"Changed the volume to {value}%.")

@bot.slash_command(
    name="stop_download",
    description="Stop the downloading process"
)
@commands.guild_only()
async def stop_downloading(ctx: discord.ApplicationContext):
    if not ALL_GUILD_ACTIVE_DOWNLOAD_MARKERS.get(ctx.guild_id, False):
        await ctx.respond("No songs are being downloaded right now.")
        return
    
    global _stop_downloading_interaction
    _stop_downloading_interaction = await ctx.respond("Trying to stop the download of remaining songs...")
    
    counter, pattern = 0, [1, 2, 3]
    while True:
        await asyncio.sleep(1)
        if not _stop_downloading_interaction:
            break
        await ctx.edit(content=f"Trying to stop the download of remaining songs{'.' * pattern[counter % 3]}")
        counter += 1
    

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
    guild_id = ctx.guild_id
    counter_for_added_songs = 0
    responded = False  # set to true for ctx.respond's that do not return immediately after
    silent_mode = False  # whether to respond with updates, is turned on when re-downloading songs in a playlist
    
    global added_song
    added_song = {}
    
    def add_archive_id(element: str):
        added_song["archive_id"] = element
    
    ALL_GUILD_SONG_QUEUES.setdefault(guild_id, [])
    ALL_GUILD_VOLUME_SETTINGS.setdefault(guild_id, DEFAULT_BOT_VOLUME)
    ALL_GUILD_DOWNLOAD_ARCHIVES.set_callback(add_archive_id, overwrite=False)
    
    if url and search_terms:
        await ctx.respond("Don't use both parameters at the same time.", ephemeral=True)
        return

    if ctx.author.voice:
        channel = ctx.author.voice.channel
        if ctx.voice_client and ctx.voice_client.is_connected():
            if channel != ctx.voice_client.channel:
                if url or search_terms or is_active(ctx):
                    await ctx.voice_client.move_to(channel)
                    if not (url or search_terms):
                        await ctx.respond("Continuing playback in your new voice channel!")
                        return
        else:
            if url or search_terms:
                try:
                    await channel.connect(timeout=2)
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
            ALL_GUILD_SONG_QUEUES[guild_id][0]["starting_time"] = datetime.now() - ALL_GUILD_SONG_QUEUES[guild_id][0]["passed_time_until_pause"]
            await ctx.respond("Playback resumed.")
        else:
            await ctx.respond("No audio is currently paused.")
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
            await message.edit(content="Started downloading the next song!")
        elif followup_message:
            followup_message = await ctx.send_followup("Started downloading the next song!", wait=True)
            message = followup_message
        else:
            await message.edit(content="Started downloading!")
        
        while True:
            await asyncio.sleep(1)
            if download_dict['status'] == 'finished':
                break
            total_bytes = download_dict.get('total_bytes', download_dict.get('total_bytes_estimate', 1))
            progress = f"{(download_dict.get('downloaded_bytes', 0) / total_bytes):.0%}" if total_bytes > 1 else "Unknown"
            await message.edit(content=f"- Progress: {progress}\n- Time left (estimate): {timedelta(seconds=download_dict.get('eta', 0))}\n- Elapsed time: {str(timedelta(seconds=download_dict['elapsed'])).split('.')[0]}")
            await asyncio.sleep(4)
        
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
        
        await message.edit(content="Download has finished, finalizing...")
        
        counter, pattern = 0, [1, 2, 3]
        while True:
            await asyncio.sleep(1)
            if processing_dict['status'] == 'finished' and processing_dict['postprocessor'] == 'MoveFiles':
                break
            await message.edit(content=f"Download has finished, finalizing{'.' * pattern[counter % 3]}")
            counter += 1
        processing_started = False
        
        if followup_message:  # Don't print song info if we're at index >= 2 of playlist
            return
        followup_message = True  # if we're calling the download_reporter again, the followup_message should be active
        
        sleep_duration = 0
        while not added_song:
            await asyncio.sleep(0.1)
            sleep_duration += 0.1
            if sleep_duration >= 5:  # safeguard, don't wait too long in case of bugs/errors
                logging.error("added_song wasn't populated in time. No longer wait for it to change.")
                await ctx.edit(content="Error upon adding your song(s) to the queue. Please try again.")
                return
        
        queue_length = len(ALL_GUILD_SONG_QUEUES.get(guild_id, []))
        if queue_length == 1:
            await ctx.edit(content=f"Queue is empty, [{added_song['title']}]({added_song["song_link"]}) started to play.")
        elif queue_length == 0:
            await ctx.edit(content="Error upon adding your song(s) to the queue. Please try again.")
        else:
            await ctx.edit(content=f"[{added_song['title']}]({added_song["song_link"]}) was added to the queue at position **{len(ALL_GUILD_SONG_QUEUES[guild_id])}**.")
    
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
        global added_song
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
            
            filename = ydl.prepare_filename(info_dict)
            mp3_filename = filename.rsplit('.', 1)[0] + '.mp3'

            if not os.path.isfile(mp3_filename):
                os.rename(filename, mp3_filename)

            added_song = {
                'archive_id': "",
                'id': info_dict.get("id"),
                'filename': mp3_filename,
                'title': info_dict.get('title'),
                'song_link': info_dict.get('webpage_url'),
                'duration_string': info_dict.get('duration_string'),
                'duration': info_dict.get('duration')
            }
            ALL_GUILD_SONG_QUEUES[guild_id].append(added_song)
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
        'download_archive': ALL_GUILD_DOWNLOAD_ARCHIVES,
        'format': 'bestaudio/best',
        'ignoreerrors': True,
        'logger': YTDLPLogger(guild_id),
        'match_filter': download_control,
        #'ratelimit': 250000,
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
        #'verbose': True
    }
    
    def download_songs(_url = url):
        nonlocal ydl
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            return ydl.extract_info(_url or f"ytsearch:{search_terms}")
    
    info_dict = None
    
    global _stop_downloading_interaction
    was_cancelled = False
    try:
        ALL_GUILD_ACTIVE_DOWNLOAD_MARKERS[guild_id] = True
        info_dict = await asyncio.to_thread(download_songs)
    except yt_dlp.utils.DownloadCancelled:
        message = _stop_downloading_interaction
        _stop_downloading_interaction = None
        await message.edit(content="Stopped downloading the remaining song(s)!")
        was_cancelled = True
    finally:
        ALL_GUILD_ACTIVE_DOWNLOAD_MARKERS[guild_id] = False
    
    already_downloaded = ALL_GUILD_DOWNLOAD_IDS.setdefault(guild_id, [])
    all_songs = ALL_GUILD_SONG_QUEUES[guild_id]
    add_to_queue = []
    for id in already_downloaded:
        try:
            add_to_queue.append(find_dict_by_id(all_songs, id)[0])
        except IndexError:
            pass
    
    del ALL_GUILD_DOWNLOAD_IDS[guild_id]

    was_error = True
    for song in add_to_queue:
        if song.get("archive_id") in ALL_GUILD_DOWNLOAD_ARCHIVES:  # song is still present in the downloads
            ALL_GUILD_SONG_QUEUES[guild_id].append(song)
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
        response = (f"Finished downloading the playlist. {counter_for_added_songs}{"" if was_cancelled else (f" / {playlist_count}")} " +
                    "songs were added to the queue.")
        if not was_cancelled and counter_for_added_songs < playlist_count and counter_for_added_songs < playlist_limit:
            response += f"\n\nAn error occurred. Make sure that no song is longer than **{SONG_MAX_LENGTH_MINUTES} minutes or age-restricted**, and try again."
        if isinstance(followup_message, discord.WebhookMessage):  # responded to its initial message earlier in the download process, edit the response
            await followup_message.edit(content=response)
        else:
            await ctx.respond(response)
        return
    
    if not responded:
        if not was_error and len(add_to_queue) > 0:
            queue_length = len(ALL_GUILD_SONG_QUEUES[guild_id])
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
        await ctx.respond("There is nothing to loop.")
        return
    
    loops = ALL_GUILD_LOOP_SETTINGS.get(ctx.guild_id, 0)
    if loops == 0:
        ALL_GUILD_LOOP_SETTINGS[ctx.guild_id] = max_times or -1
        await ctx.respond(f"The song that is currently played will be looped {f"{max_times} time{'s' if max_times > 1 else ''}" if max_times else "infinitely"}.")
    else:
        ALL_GUILD_LOOP_SETTINGS[ctx.guild_id] = max_times or 0
        await ctx.respond(f"Song will be looped {max_times} more time{'s' if max_times > 1 else ''}." if max_times else "Disabled looping for this song.")
        

@bot.slash_command(
    name="info",
    description="Infos about the current song"
)
@commands.guild_only()
async def info(ctx: discord.ApplicationContext):
    if not is_active(ctx):
        await ctx.respond("There is no song currently playing.")
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

    for i in range(1, len(ALL_GUILD_SONG_QUEUES[ctx.guild_id])):
        song = ALL_GUILD_SONG_QUEUES[ctx.guild_id][i]

        if i == cutoff:
            response += f"- ...{len(ALL_GUILD_SONG_QUEUES[ctx.guild_id]) - cutoff} more song(s).\n"
            break

        duration_string = song["duration_string"]
        if song["duration"] < 60:  # if song is under 1 minute, duration_string is just the number of seconds
            duration_string = "0:" + duration_string.zfill(2)
        if song["duration"] < 60 * 60:  # song is shorter than 1 hour
            placeholder = "0:00"
        else:
            placeholder = "0:00:00"
            
        response += f"- [{song["title"]}](<{song["song_link"]}>) - ({placeholder} / {duration_string})\n"
        
    if ALL_GUILD_ACTIVE_DOWNLOAD_MARKERS.get(ctx.guild_id, False):
        response += "\n..._more songs are currently being downloaded_..."
    
    await ctx.respond(response)

@bot.slash_command(
    name="clear_queue",
    description="Stop playback and clear entire queue"
)
@commands.guild_only()
async def clear_queue(ctx: discord.ApplicationContext):
    if not is_active(ctx):
        await ctx.respond("Queue already empty.")
        return
    
    cleanup(ctx.guild_id)
    ctx.voice_client.stop()
    
    await ctx.respond("Stopped playback and cleared the queue.")

@bot.slash_command(
    name="skip",
    description="Skip the current song"
)
@commands.guild_only()
async def skip(ctx: discord.ApplicationContext):
    if is_active(ctx):
        ALL_GUILD_LOOP_SETTINGS[ctx.guild_id] = 0
        ctx.voice_client.stop()
        await ctx.respond("Song skipped.")
    else:
        await ctx.respond("No audio is currently playing.")

@bot.slash_command(
    name="pause",
    description="Pause the current playback"
)
@commands.guild_only()
async def pause(ctx: discord.ApplicationContext):
    if ctx.voice_client and ctx.voice_client.is_playing():
        ctx.voice_client.pause()
        ALL_GUILD_SONG_QUEUES[ctx.guild_id][0]["passed_time_until_pause"] = datetime.now() - ALL_GUILD_SONG_QUEUES[ctx.guild_id][0]["starting_time"]
        await ctx.respond("Playback paused.")
    else:
        await ctx.respond("No audio is currently playing.")

##################################################################
############################ RUN BOT #############################
##################################################################

@bot.listen
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    if after.channel:
        if member == bot.user:
            ALL_GUILD_CURRENT_VOICE_CHANNEL_IDS[after.channel.guild.id] = after.channel.id
            return
    
    if not after.channel and member == bot.user:
        guild_id = before.channel.guild.id
        cleanup(guild_id)
        return
    
    if (before.channel and before.channel.id == ALL_GUILD_CURRENT_VOICE_CHANNEL_IDS.get(before.channel.guild.id, None) and
        len(before.channel.members) == 1 and before.channel.members[0] == bot.user):
        bot.loop.create_task(disconnect_countdown(before.channel))

@bot.listen(once=True)
async def on_ready():
    global ALL_GUILD_VOLUME_SETTINGS
    # initialize json
    try:
        with open(VOLUME_SETTINGS_FILE_PATH, 'r') as file:
            ALL_GUILD_VOLUME_SETTINGS = json.load(file, object_pairs_hook=lambda pairs: {int(k): v for k,v in pairs})
    except (OSError, json.JSONDecodeError) as e:
        logging.error(f"Error upon reading {VOLUME_SETTINGS_FILE_PATH}: {e}")
        pass
    
    logging.info(f'Logged in as {bot.user}')


bot.run(config.DISCORD_TOKEN)

# TODO: untersuchen, warum ffmpeg -9 bei /skip kommt
# namen der variablen korrigieren (constants etc., private markieren), gucken ob ' und " vereinheitlicht werden sollten