import asyncio
from datetime import datetime, timedelta
import json
import logging
import os
import re

import discord
from discord.ext import commands
from discord import option
import yt_dlp

import config

logging.basicConfig(level=logging.INFO, format='[%(asctime)s] [%(levelname)s]: %(message)s', handlers=[
    logging.FileHandler('babshoven.log'),
    logging.StreamHandler()
])

bot = commands.Bot()

##################################################################
############################ GENERAL #############################
##################################################################

def create_embed(title=None, description=None, color=None, footer=None):
    embed_var = discord.Embed(title=title, description=description, color=color)
    embed_var.set_footer(text=footer)
    return embed_var


def is_active(ctx: discord.ApplicationContext):
    return ctx.voice_client and (ctx.voice_client.is_playing() or ctx.voice_client.is_paused())


# Is called when the bot is asked to leave/clear its storage/refresh its state. Clears song queue, resets loop parameter, etc.
def cleanup(guild_id: int):
    ALL_GUILD_SONG_QUEUES.pop(guild_id, None)
    ALL_GUILD_LOOP_SETTINGS.pop(guild_id, None)

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

ALL_GUILD_SONG_QUEUES: dict[int, list[dict[str, str | int]]] = {}
ALL_GUILD_CURRENT_SONGS: dict[int, dict[str, str | int | datetime | timedelta]] = {}
SONG_MAX_LENGTH_MINUTES = 30

ALL_GUILD_LOOP_SETTINGS: dict[int, int] = {}  # (guild_id: -n | 0 | +n) -> -n: loop infinite, otherwise +n times


def current_song_info(ctx: discord.ApplicationContext):
    response = ""
    current_song = ALL_GUILD_CURRENT_SONGS.get(ctx.guild_id)
    
    if not current_song:
        return ""

    if ctx.voice_client.is_playing():
        runtime = str(datetime.now() - current_song["starting_time"]).split('.')[0]
    elif ctx.voice_client.is_paused():
        response += "# [Playback is paused]\n"
        runtime = str(current_song["passed_time_until_pause"]).split('.')[0]
    else:
        runtime = "0:00:00"
        
    if current_song["duration"] < 60 * 60:  # song is shorter than 1 hour
        runtime = runtime.removeprefix("0:").removeprefix("0")
        
    response += f"- **[{current_song["title"]}](<{current_song["video_link"]}>) - ({runtime} / {current_song["duration_string"]})**\n"
    
    return response
    

# Delete the last played song if it's not in any song queue anymore.
def remove_downloaded_song(current_song: dict[str, str | int | datetime]):
    if not current_song:
        return
    
    # check if any of the song queues contains the filename
    filename = current_song['filename']
    if not any(song['filename'] == filename for queue in ALL_GUILD_SONG_QUEUES.values() for song in queue):
        try:
            os.remove(filename)
        except FileNotFoundError:
            pass

# Function is called every time a song finishes to be able to start the next one from the queue.
async def play_next(ctx: discord.ApplicationContext):
    guild_id = ctx.guild_id
    volume = ALL_GUILD_VOLUME_SETTINGS.get(guild_id, DEFAULT_BOT_VOLUME)    
    loops = ALL_GUILD_LOOP_SETTINGS.get(guild_id, 0)
    
    if loops == 0:  # try to remove song only if it's not actively being looped
        remove_downloaded_song(ALL_GUILD_CURRENT_SONGS.get(guild_id))
    
    if len(ALL_GUILD_SONG_QUEUES[guild_id]) == 0:  # early exit if there is no song in queue
        return

    ALL_GUILD_CURRENT_SONGS[guild_id] = ALL_GUILD_SONG_QUEUES[guild_id].pop(0)
    if loops != 0:
        ALL_GUILD_SONG_QUEUES[guild_id].insert(0, ALL_GUILD_CURRENT_SONGS[guild_id])
        ALL_GUILD_LOOP_SETTINGS[guild_id] -= 1
        
    ALL_GUILD_CURRENT_SONGS[guild_id]["starting_time"] = datetime.now()
    
    source = await discord.FFmpegOpusAudio.from_probe(
        ALL_GUILD_CURRENT_SONGS[guild_id]['filename'], method='fallback', options=f"-af 'volume={volume}'")
    ctx.voice_client.play(source, after=lambda e: asyncio.run_coroutine_threadsafe(play_next(ctx), bot.loop))

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

# TODO: make it apply to the current song, maybe with force parameter (play the song again, fast forward to current timestamp)
@bot.slash_command(
    name="volume",
    description=f"Adjusts the volume (doesn't apply to the current song)"
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
    if not value:
        current_volume = (ALL_GUILD_VOLUME_SETTINGS.get(ctx.guild_id, DEFAULT_BOT_VOLUME)) * 100
        await ctx.respond(f"Volume currently is set to {int(current_volume)}%.")
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
    
    await ctx.respond(f"Changed the volume to {value}%.")

@bot.slash_command(
    name="play",
    description="Add a YouTube video to the queue"
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
    
    ALL_GUILD_SONG_QUEUES.setdefault(guild_id, [])
    ALL_GUILD_VOLUME_SETTINGS.setdefault(guild_id, DEFAULT_BOT_VOLUME)

    if bool(url) == bool(search_terms):
        await ctx.respond("You need to provide either an URL or search terms.", ephemeral=True)
        return

    if not ctx.voice_client:
        if ctx.author.voice:
            channel = ctx.author.voice.channel
            await channel.connect()
        else:
            await ctx.respond("You are not in a voice channel!", ephemeral=True)
            return
    
    if url and re.search(r"^(?:https?:\/\/(?:www\.)?)?(?:(?:youtube\.com)|(?:youtu\.be))", url) is None:
        await ctx.respond("Currently, only YouTube is supported.", ephemeral=True)
        return 

    await ctx.defer()
    
    def vid_too_long(info, *, incomplete):
        global is_vid_too_long
        is_vid_too_long = False
        duration = info.get('duration')
        if (duration and duration > SONG_MAX_LENGTH_MINUTES * 60):
            is_vid_too_long = True
            return f"'{info.get('title')}' is too long"

    ydl_opts = {
        'format': 'bestaudio/best',
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '192',
        }],
        'match_filter': vid_too_long,
        'noplaylist': True,
        'playlist_items': '1',
        'outtmpl': 'downloads/%(title)s.%(ext)s',
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        try:
            info_dict = ydl.extract_info(url or f"ytsearch:{search_terms}")
        except yt_dlp.utils.DownloadError:
            await ctx.respond("An error occurred. Please try again, and make sure the video is not age-restricted.")
            return
        
        if is_vid_too_long:
            await ctx.respond(f"Video must be shorter than {SONG_MAX_LENGTH_MINUTES} minutes.")
            return

        if search_terms:
            info_dict = info_dict.get('entries')[0]
        
        filename = ydl.prepare_filename(info_dict)
        mp3_filename = filename.rsplit('.', 1)[0] + '.mp3'

        if not os.path.isfile(mp3_filename):
            os.rename(filename, mp3_filename)
        
    song: dict[str, str | int] = {
        'filename': mp3_filename,
        'title': info_dict.get('title'),
        'video_link': info_dict.get('webpage_url'),
        'duration_string': info_dict.get('duration_string'),
        'duration': info_dict.get('duration')
    }
    ALL_GUILD_SONG_QUEUES[guild_id].append(song)

    video_link = info_dict.get('webpage_url')
    
    if len(ALL_GUILD_SONG_QUEUES[guild_id]) == 1 and not is_active(ctx):
        await ctx.respond(f"Queue is empty, [{song['title']}]({video_link}) is about to be played.")
    else:
        await ctx.respond(f"[{song['title']}]({video_link}) was added to the queue at position **{len(ALL_GUILD_SONG_QUEUES[guild_id]) + 1}**.")

    if ctx.voice_client and not is_active(ctx):
        await play_next(ctx)


#TODO: loop is bugged, only plays once, after fixing make sure that /play and /queue are aware of the loops
#@bot.slash_command(
#    name="loop",
#    description="Loops the current song or stops the loop"
#)
#@option(
#    "max_times",
#    description="Maximum number of times this song will be looped; infinite if omitted",
#    required=False,
#    input_type=int,
#    min_value=1
#)
async def loop(ctx: discord.ApplicationContext, max_times: int):
    if not ctx.voice_client or not ctx.voice_client.is_playing():
        await ctx.respond("There is nothing to loop.")
        return
    
    loops = ALL_GUILD_LOOP_SETTINGS.get(ctx.guild_id)
    if not loops or loops == 0:
        ALL_GUILD_LOOP_SETTINGS[ctx.guild_id] = max_times or -1
        await ctx.respond(f"The song that is currently played will be looped {f"{max_times} times" if max_times else "infinitely"}.")
    else:
        ALL_GUILD_LOOP_SETTINGS[ctx.guild_id] = 0
        await ctx.respond("Disabled looping for this song.")
        

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
    
    response = current_song_info(ctx)

    for i in range(0, len(ALL_GUILD_SONG_QUEUES[ctx.guild_id])):
        song = ALL_GUILD_SONG_QUEUES[ctx.guild_id][i]

        if i == cutoff:
            response += f"- ...{len(ALL_GUILD_SONG_QUEUES[ctx.guild_id]) - cutoff} more song(s)."
            break

        if song["duration"] < 60 * 60:  # song is shorter than 1 hour
            placeholder = "0:00"
        else:
            placeholder = "0:00:00"
            
        response += f"- [{song["title"]}](<{song["video_link"]}>) - ({placeholder} / {song["duration_string"]})\n"
    
    await ctx.respond(response)

@bot.slash_command(
    name="clear_queue",
    description="Stop playback and clear entire queue"
)
async def clear_queue(ctx: discord.ApplicationContext):
    if not is_active(ctx):
        await ctx.respond("Queue already empty.")
        return
    
    ctx.voice_client.stop()
    cleanup(ctx.guild_id)
    
    await ctx.respond("Stopped playback and cleared the queue.")

@bot.slash_command(
    name="skip",
    description="Skips the current song"
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
    description="Pauses the current playback"
)
async def pause(ctx: discord.ApplicationContext):
    if ctx.voice_client and ctx.voice_client.is_playing():
        ctx.voice_client.pause()
        ALL_GUILD_CURRENT_SONGS[ctx.guild_id]["passed_time_until_pause"] = datetime.now() - ALL_GUILD_CURRENT_SONGS[ctx.guild_id]["starting_time"]
        await ctx.respond("Playback paused.")
    else:
        await ctx.respond("No audio is currently playing.")

@bot.slash_command(
    name="resume",
    description="Resumes the current playback"
)
async def resume(ctx: discord.ApplicationContext):
    if ctx.voice_client and ctx.voice_client.is_paused():
        ctx.voice_client.resume()
        ALL_GUILD_CURRENT_SONGS[ctx.guild_id]["starting_time"] = datetime.now() - ALL_GUILD_CURRENT_SONGS[ctx.guild_id]["passed_time_until_pause"]
        await ctx.respond("Playback resumed.")
    else:
        await ctx.respond("No audio is currently paused.")

##################################################################
############################ RUN BOT #############################
##################################################################

@bot.listen
async def on_voice_state_update(member, before, after):
    if not after.channel and member == bot.user:
        guild_id = before.channel.guild.id
        cleanup(guild_id)

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

#TODO: loop song command, allow playlists, apply volume instantly by maybe restarting the song and fast forwarding to the current timestamp?
# download der songs async machen, wegen 10s heartbeat block https://stackoverflow.com/questions/65881761/discord-gateway-warning-shard-id-none-heartbeat-blocked-for-more-than-10-second
# untersuchen, warum ffmpeg -9 bei /skip kommt
