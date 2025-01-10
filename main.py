import asyncio
from datetime import datetime, timedelta
import json
import logging
import os
import re
import time

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
            ALL_GUILD_DOWNLOAD_IDS[self.guild_id] = msg.split(':')[0].removeprefix("[download] ")[len("[0;32m"):-len("[0m")]
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

bot = commands.Bot()

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
    ALL_GUILD_SONG_QUEUES.pop(guild_id, None)
    ALL_GUILD_LOOP_SETTINGS.pop(guild_id, None)


def find_dict_by_id(list: list[dict[str, str | int | datetime | timedelta]], id: str):
    filtered_list = filter(lambda d: bool(d), list)  # if there are empty dicts in list (error handling purposes), filter those out
    return filter(lambda d: d["id"] == id, filtered_list)


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

@bot.event
async def on_application_command_error(ctx: discord.ApplicationContext, error: discord.DiscordException):
    logging.error(error)
    raise error

##################################################################
######################### MUSIC METHODS ##########################
##################################################################

DEFAULT_BOT_VOLUME = 0.2
ALL_GUILD_VOLUME_SETTINGS: dict[int, float] = {}
VOLUME_SETTINGS_FILE_PATH = "./volumesettings.json"

PAUSE_AFTER_PLAY: dict[int, bool] = {}

ALL_GUILD_DOWNLOAD_IDS: dict[int, str] = {}  # contains id of the most recent song that was tried to be downloaded, but denied due to already being present in download_archive
ALL_GUILD_CURRENT_ARCHIVE_IDS: dict[int, str] = {}  # contains entry that's added to the download_archive
ALL_GUILD_DOWNLOAD_ARCHIVES: ObservableSet = ObservableSet()
ALL_GUILD_SONG_QUEUES: dict[int, list[dict[str, str | int | timedelta]]] = {}
ALL_GUILD_CURRENT_SONGS: dict[int, dict[str, str | int | datetime | timedelta]] = {}
SONG_MAX_LENGTH_MINUTES = 30

ALL_GUILD_LOOP_SETTINGS: dict[int, int] = {}  # (guild_id: -n | 0 | +n) -> -n: loop infinite, otherwise +n times


def is_active(ctx: discord.ApplicationContext):
    return ctx.voice_client and (ctx.voice_client.is_playing() or ctx.voice_client.is_paused())


def current_song_info(ctx: discord.ApplicationContext):
    response = ""
    current_song = ALL_GUILD_CURRENT_SONGS.get(ctx.guild_id)
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
        
    response += f"- **[{current_song["title"]}](<{current_song["video_link"]}>) - ({runtime} / {duration_string})"
    
    if loops != 0:
        response += f" [Looped: {loops if loops > 0 else '\u221e'} time{'s' if loops != 1 else ''} left]"
    
    return response + "**"
    

# Delete the last played song if it's not in any song queue anymore.
def remove_downloaded_song(ctx: discord.ApplicationContext, current_song: dict[str, str | int | datetime]):
    if not current_song:
        return
    
    ALL_GUILD_CURRENT_SONGS.pop(ctx.guild_id)
    
    # check if any of the song queues contains the filename
    filename = current_song['filename']
    all_songs = list(ALL_GUILD_SONG_QUEUES.values()) + list(list(ALL_GUILD_CURRENT_SONGS.values()))
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
        # try to remove song only if it's not actively being looped - the queue already might be empty
        remove_downloaded_song(ctx, ALL_GUILD_CURRENT_SONGS.get(guild_id))
        if len(ALL_GUILD_SONG_QUEUES[guild_id]) == 0:
            return
        
        try:
            ALL_GUILD_CURRENT_SONGS[guild_id] = ALL_GUILD_SONG_QUEUES[guild_id].pop(0)
        except IndexError:
            pass
    else:
         ALL_GUILD_LOOP_SETTINGS[guild_id] -= 1

    passed_time = ALL_GUILD_CURRENT_SONGS[guild_id].get("passed_time", timedelta(seconds=0))
    ALL_GUILD_CURRENT_SONGS[guild_id]["starting_time"] = datetime.now() - passed_time

    source = await discord.FFmpegOpusAudio.from_probe(
        ALL_GUILD_CURRENT_SONGS[guild_id]['filename'], method='fallback',
            before_options=f"-ss {str(passed_time)}", options=f"-af 'volume={volume}'")
    
    ALL_GUILD_CURRENT_SONGS[guild_id]["passed_time"] = timedelta(seconds=0)  # reset passed_time in case of loops
    
    ctx.voice_client.play(source, after=lambda e: asyncio.run_coroutine_threadsafe(play_next(ctx), bot.loop))
    if PAUSE_AFTER_PLAY.get(guild_id, False):
        ctx.voice_client.pause()
        PAUSE_AFTER_PLAY[guild_id] = False
    

##################################################################

@bot.slash_command(
    name="leave",
    description="Leave the voice channel"
)
async def leave(ctx: discord.ApplicationContext):
    if ctx.voice_client:
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
            passed_time = datetime.now() - ALL_GUILD_CURRENT_SONGS[ctx.guild_id]["starting_time"]
            ALL_GUILD_CURRENT_SONGS[ctx.guild_id]["passed_time"] = passed_time
        elif ctx.voice_client.is_paused():
            ALL_GUILD_CURRENT_SONGS[ctx.guild_id]["passed_time"] = ALL_GUILD_CURRENT_SONGS[ctx.guild_id]["passed_time_until_pause"]
            PAUSE_AFTER_PLAY[ctx.guild_id] = True
        
        if is_active(ctx):
            loops = ALL_GUILD_LOOP_SETTINGS.get(ctx.guild_id, 0)
            ALL_GUILD_LOOP_SETTINGS[ctx.guild_id] = loops + 1 if loops >= 0 else loops
            ctx.voice_client.stop()
    
    await ctx.respond(f"Changed the volume to {value}%.")

@bot.slash_command(
    name="play",
    description="Add a YouTube video to the queue or resume paused playback (if both parameters are left empty)"
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
async def play(ctx: discord.ApplicationContext, url: str, search_terms: str):
    guild_id = ctx.guild_id
    
    def add_archive_id(element: str):
        ALL_GUILD_CURRENT_ARCHIVE_IDS[guild_id] = element
    
    ALL_GUILD_SONG_QUEUES.setdefault(guild_id, [])
    ALL_GUILD_VOLUME_SETTINGS.setdefault(guild_id, DEFAULT_BOT_VOLUME)
    ALL_GUILD_DOWNLOAD_ARCHIVES.set_callback(add_archive_id, overwrite=False)
    
    if url and search_terms:
        await ctx.respond("Don't use both parameters at the same time.", ephemeral=True)
        return

    if ctx.author.voice:
        channel = ctx.author.voice.channel
        if ctx.voice_client:
            if channel != ctx.voice_client.channel:
                if not url and not search_terms and is_active(ctx):
                    await ctx.voice_client.move_to(channel)
                    if ctx.voice_client.is_playing():
                        await ctx.respond("Continuing playback in your new voice channel!")
                        return
        else:
            if url or search_terms:
                await channel.connect()
    else:
        await ctx.respond("You are not in a voice channel!", ephemeral=True)
        return
    
    if not url and not search_terms:
        if ctx.voice_client and ctx.voice_client.is_paused():
            ctx.voice_client.resume()
            ALL_GUILD_CURRENT_SONGS[ctx.guild_id]["starting_time"] = datetime.now() - ALL_GUILD_CURRENT_SONGS[ctx.guild_id]["passed_time_until_pause"]
            await ctx.respond("Playback resumed.")
        else:
            await ctx.respond("No audio is currently paused.")
        return
    
    if url and re.search(r"^(?:https?:\/\/(?:www\.)?)?(?:(?:youtube\.com)|(?:youtu\.be))", url) is None:
        await ctx.respond("Currently, only YouTube is supported.", ephemeral=True)
        return 

    await ctx.defer()
    # TODO: bei playlists: wenn is_playing() oder vllt wenn queue länge > 0, keine ctx.respond und .edit, er soll im hintergrund die vids laden
    # ganz am ende bei playlist länge > 1 nochmal ne nachricht "alles geladen und vorbereitet" raushauen (noch ein .respond)
    download_dict, processing_dict = {}, {}
    async def download_reporter():
        await ctx.respond("Started the download!")
        while download_dict['status'] != 'finished':
            await asyncio.sleep(5)
            total_bytes = download_dict.get('total_bytes', download_dict.get('total_bytes_estimate', 1))
            progress = f"{(download_dict.get('downloaded_bytes', 0) / total_bytes):.0%}" if total_bytes > 1 else "Unknown"
            await ctx.edit(content=f"- Progress: {progress}\n- ETA: {timedelta(seconds=download_dict.get('eta', 0))}\n- Elapsed time: {str(timedelta(seconds=download_dict['elapsed'])).split('.')[0]}")

    async def processing_reporter():
        await ctx.edit(content="Download has finished, your song will be played shortly!")
        counter, pattern = 0, [1, 2, 3, 2]
        while True:
            await asyncio.sleep(2)
            if processing_dict['status'] == 'finished' and processing_dict['postprocessor'] == 'MoveFiles':
                break  # using break, because the while condition is not always respected for some reason
            await ctx.edit(content=f"Post-processing{'.' * pattern[counter % 4]}")
            counter += 1
        
    downloading_started, processing_started = False, False
    def download_hooks(d):
        nonlocal downloading_started, download_dict
        download_dict = d
        if d['status'] == 'downloading':
            if not downloading_started:
                bot.loop.create_task(download_reporter())
                downloading_started = True
    
    added_song = {}
    ydl = None
    def processing_hooks(d):
        nonlocal processing_started, processing_dict, added_song, ydl
        processing_dict = d
        if d['status'] == 'started':
            if not processing_started:
                bot.loop.create_task(processing_reporter())
                processing_started = True
        if d['status'] == 'finished' and d['postprocessor'] == 'MoveFiles':
            # das klappt bei einem song per url. Search terms brauch das info_dict = info_dict.entries[0] von unten
            # TODO: wird der bumms hier pro video aufgerufen oder pro playlist? und was ist genau im info_dict? immer nur das aktuelle video oder die gesamte playlist bist jetzt?
            info_dict = d['info_dict']
            
            if not info_dict:  # should never happen, but you can't be too careful
                return
            
            if search_terms:  # TODO: checken ob das im playlist fall immer noch so ist mit den entries
                try:
                    info_dict = info_dict.get('entries')[0]
                except (AttributeError, IndexError):
                    return
            
            filename = ydl.prepare_filename(info_dict)
            mp3_filename = filename.rsplit('.', 1)[0] + '.mp3'

            if not os.path.isfile(mp3_filename):
                os.rename(filename, mp3_filename)
            
            added_song = {
                'archive_id': ALL_GUILD_CURRENT_ARCHIVE_IDS.get(guild_id, ""),
                'id': info_dict.get("id"),
                'filename': mp3_filename,
                'title': info_dict.get('title'),
                'video_link': info_dict.get('webpage_url'),
                'duration_string': info_dict.get('duration_string'),
                'duration': info_dict.get('duration')
            }
            ALL_GUILD_SONG_QUEUES[guild_id].append(added_song)
                
    
    is_vid_too_long = False
    def vid_too_long(info, *, incomplete):
        nonlocal is_vid_too_long
        is_vid_too_long = False
        duration = info.get('duration')
        if (duration and duration > SONG_MAX_LENGTH_MINUTES * 60):
            is_vid_too_long = True
            return f"'{info.get('title')}' is too long"

    ydl_opts = {
        'download_archive': ALL_GUILD_DOWNLOAD_ARCHIVES,
        'format': 'bestaudio/best',
        'logger': YTDLPLogger(guild_id),
        'match_filter': vid_too_long,
        #'ratelimit': 500000,
        'noplaylist': True,
        'paths': {'home': 'downloads/'},
        'progress_hooks': [download_hooks],
        'postprocessor_hooks': [processing_hooks],
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '192',
        }],
        #'verbose': True
        #TODO: add hooks to call when downloads/porecessing is finished and the vid can be added to queue
        #TODO: vermutlich kommt alle paar sekunden ne nachricht dass der playlist ein song hinzugefügt wurde. Möglichkeit finden, das abzubrechen oder von vornherein max anzahl angeben
    }
    
    def download_videos():
        nonlocal ydl
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.extract_info(url or f"ytsearch:{search_terms}")
    
    try:
        await asyncio.to_thread(download_videos)
    except yt_dlp.utils.DownloadError as e:
        logging.error(f"Download of video failed: {e}")
        await ctx.respond("An error occurred. Please try again, and make sure the video is not age-restricted.")
        #TODO: playlist behavoir: what happens with errors in the playlist? -> desired: skip after 1 error
        #TODO: what happens when an additional /play is entered while the bot is downloading?
        return
    
    if is_vid_too_long:
        # TODO: check playlist behavior
        await ctx.respond(f"Video must be shorter than {SONG_MAX_LENGTH_MINUTES} minutes.")
        return
    
    # TODO: sammel alle IDs bei denen es bereits im archive ist (über den console output)
    # hier am ende alle geblockten IDs durchgehen und die in die Queue packen, wenn sie noch gedownloaded sind (checken)
    # in einer playlist sollte ein geblockter song einfach übergangen werden, als einzelvideo muss ne extra nachricht kommen
    already_downloaded = ALL_GUILD_DOWNLOAD_IDS.get(guild_id, [])  # TODO: auf dict[int, list[str]] umschreiben, das die neue id appended bekommt. am ende von /play liste leeren.
    
    
    # info_dict is None, most likely due to download_archive blocking the download. Search the queue and use the song info that's already there
    added_song = list(find_dict_by_id(ALL_GUILD_SONG_QUEUES.get(guild_id, []) + [ALL_GUILD_CURRENT_SONGS.get(guild_id, {})],
                                ALL_GUILD_DOWNLOAD_IDS.get(guild_id, "")))
    if len(added_song) == 0:  # function hasn't found anything. Abort.
        await ctx.edit(content="An error occurred. Please try again, and optionally clear the queue.")
        return
    added_song = added_song[0]
    ALL_GUILD_SONG_QUEUES[guild_id].append(added_song)
    
    if len(ALL_GUILD_SONG_QUEUES[guild_id]) == 1 and not is_active(ctx):
        await ctx.edit(content=f"Queue is empty, [{added_song['title']}]({added_song["video_link"]}) is about to be played.")
    else:
        await ctx.edit(content=f"[{added_song['title']}]({added_song["video_link"]}) was added to the queue at position **{len(ALL_GUILD_SONG_QUEUES[guild_id]) + 1}**.")

    if ctx.voice_client and not is_active(ctx):
        await play_next(ctx)


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
async def info(ctx: discord.ApplicationContext):
    response = current_song_info(ctx)
    if response == "":
        await ctx.respond("Error while retrieving song info. Please try again.", ephemeral=True)
    else:
        await ctx.respond(response)


@bot.slash_command(
    name="queue",
    description="Details about the currently playing song and the queue"
)
async def queue(ctx: discord.ApplicationContext):
    if not is_active(ctx):
        await ctx.respond("There are currently no songs in queue.")
        return
    
    cutoff = 5
    
    response = current_song_info(ctx) + '\n'

    for i in range(0, len(ALL_GUILD_SONG_QUEUES[ctx.guild_id])):
        song = ALL_GUILD_SONG_QUEUES[ctx.guild_id][i]

        if i == cutoff:
            response += f"- ...{len(ALL_GUILD_SONG_QUEUES[ctx.guild_id]) - cutoff} more song(s)."
            break

        duration_string = song["duration_string"]
        if song["duration"] < 60:  # if song is under 1 minute, duration_string is just the number of seconds
            duration_string = "0:" + duration_string.zfill(2)
        if song["duration"] < 60 * 60:  # song is shorter than 1 hour
            placeholder = "0:00"
        else:
            placeholder = "0:00:00"
            
        response += f"- [{song["title"]}](<{song["video_link"]}>) - ({placeholder} / {duration_string})\n"
    
    await ctx.respond(response)

@bot.slash_command(
    name="clear_queue",
    description="Stop playback and clear entire queue"
)
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
async def pause(ctx: discord.ApplicationContext):
    if ctx.voice_client and ctx.voice_client.is_playing():
        ctx.voice_client.pause()
        ALL_GUILD_CURRENT_SONGS[ctx.guild_id]["passed_time_until_pause"] = datetime.now() - ALL_GUILD_CURRENT_SONGS[ctx.guild_id]["starting_time"]
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
    
    if before.channel and before.channel.id == ALL_GUILD_CURRENT_VOICE_CHANNEL_IDS.get(before.channel.guild.id, None):
        if len(before.channel.members) == 1:
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

#TODO: allow playlists, 
# untersuchen, warum ffmpeg -9 bei /skip kommt