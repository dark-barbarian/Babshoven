import asyncio
import re
import config
import discord
import yt_dlp as youtube_dl
import os

from discord.ext import commands
from discord import option

bot = commands.Bot()

##################################################################
############################ GENERAL #############################
##################################################################

def create_embed(title=None, description=None, color=None, footer=None):
    embed_var = discord.Embed(title=title, description=description, color=color)
    embed_var.set_footer(text=footer)
    return embed_var

##################################################################

@bot.slash_command(
    name="ping",
    description="Check the bot's latency",
    #guild_ids=[248493533537763328]
    )
async def ping(ctx: discord.ApplicationContext):
    #await ctx.respond(embed=create_embed('Latency', f'{round(bot.latency * 1000)} ms', color=0x000000))
    await ctx.respond(f"Latency: {round(bot.latency * 1000)} ms")

##################################################################
######################### MUSIC METHODS ##########################
##################################################################

DEFAULT_BOT_VOLUME = 0.2
VOLUMES = {}
SONG_QUEUES = {}
CURRENT_SONG = {}

# Check if any of the song queues contains the filename or can be deleted safely.
def contains_song(filename: str):
    for guild_id in SONG_QUEUES:
        for song in SONG_QUEUES[guild_id]:
            if filename == song['filename']:
                return True
            
    return False

# Delete the last played song if it's not in any song queue anymore.
def remove_downloaded_song(ctx: discord.ApplicationContext):
    if CURRENT_SONG.get(ctx.guild.id) and not contains_song(CURRENT_SONG[ctx.guild.id]['filename']):
        os.remove(CURRENT_SONG[ctx.guild.id]['filename'])

async def play_next(ctx: discord.ApplicationContext):
    if not SONG_QUEUES[ctx.guild.id]:
        remove_downloaded_song(ctx)
        return
    
    volume = VOLUMES[ctx.guild.id] or DEFAULT_BOT_VOLUME

    remove_downloaded_song(ctx)

    CURRENT_SONG[ctx.guild.id] = SONG_QUEUES[ctx.guild.id].pop(0)
    source = await discord.FFmpegOpusAudio.from_probe(CURRENT_SONG[ctx.guild.id]['filename'], method='fallback', options=f"-af 'volume={volume}'")
    ctx.voice_client.play(source, after=lambda e: asyncio.run_coroutine_threadsafe(play_next(ctx), bot.loop))

@bot.slash_command(
    name="leave",
    description="Leave the voice channel",
    #guild_ids=[248493533537763328]
    )
async def leave(ctx: discord.ApplicationContext):
    if ctx.voice_client:
        await ctx.voice_client.disconnect()
        await ctx.respond("Left the voice channel.")
    else:
        await ctx.respond("I am not in a voice channel!")

@bot.slash_command(
    name="volume",
    description="Adjusts the volume (doesn't apply to the current song)",
    #guild_ids=[248493533537763328, 556956284159524981]
)
@option(
    "volume",
    input_type=int,
    min_value=1,
    max_value=100
)
async def volume(ctx: discord.ApplicationContext, volume: int):
    VOLUMES[ctx.guild.id] = float(volume) / 100
    await ctx.respond(f"Changed the volume to {volume}%.")

@bot.slash_command(
    name="play",
    description="Add a YouTube video to the queue",
    #guild_ids=[248493533537763328, 556956284159524981]
    )
@option(
    "url", 
    description="Link the YouTube video",
    required=False,
    default=''
)
@option(
    "search_terms", 
    description="Search for a YouTube video",
    required=False,
    default=''
)
async def play(ctx: discord.ApplicationContext, url: str, search_terms: str):
    guild_id = ctx.guild.id
    if guild_id not in SONG_QUEUES:
        SONG_QUEUES[guild_id] = []
    
    if guild_id not in VOLUMES:
        VOLUMES[guild_id] = DEFAULT_BOT_VOLUME

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
    
    if url and re.search("^(?:https?:\/\/(?:www\.)?)?(?:(?:youtube\.com)|(?:youtu\.be))", url) is None:
        await ctx.respond("Currently, only YouTube is supported.", ephemeral=True)
        return 

    await ctx.defer()

    ydl_opts = {
        'format': 'bestaudio/best',
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '192',
        }],
        'noplaylist': True,
        'playlist_items': '1',
        'outtmpl': 'downloads/%(title)s.%(ext)s',
    }

    with youtube_dl.YoutubeDL(ydl_opts) as ydl:
        info_dict = ydl.extract_info(url or f"ytsearch:{search_terms}", download=False)
        
        if (url and info_dict.get('duration') > 600) or (search_terms and info_dict.get('entries')[0].get('duration') > 600):
            await ctx.respond("Video must be shorter than 10 minutes.", delete_after=5)
            return

        info_dict = ydl.extract_info(url or f"ytsearch:{search_terms}", download=True)
        filename = (url and ydl.prepare_filename(info_dict)) or ydl.prepare_filename(info_dict.get('entries')[0])
        mp3_filename = filename.rsplit('.', 1)[0] + '.mp3'

        if not os.path.isfile(mp3_filename):
            os.rename(filename, mp3_filename)

    song = {
        'filename': mp3_filename,
        'title': url and info_dict.get('title') or info_dict.get('entries')[0].get('title'),
        'video_link': url and info_dict.get('webpage_url') or info_dict.get('entries')[0].get('webpage_url'),
        'length': url and info_dict.get('duration_string') or info_dict.get('entries')[0].get('duration_string')
    }
    SONG_QUEUES[guild_id].append(song)

    video_link = url and info_dict.get('webpage_url') or info_dict.get('entries')[0].get('webpage_url')

    if len(SONG_QUEUES[guild_id]) == 1 and not ctx.voice_client.is_playing():
        await ctx.respond(f"Queue is empty, [{song['title']}]({video_link}) is about to be played.")
    else:
        await ctx.respond(f"[{song['title']}]({video_link}) was added to the queue at position **{len(SONG_QUEUES[guild_id]) + 1}**.")

    if not ctx.voice_client.is_playing():
        await play_next(ctx)

@bot.slash_command(
    name="queue",
    description="Details about the currently playing song and the queue",
    #guild_ids=[248493533537763328, 556956284159524981]
)
async def queue(ctx: discord.ApplicationContext):
    if not ctx.voice_client or (not SONG_QUEUES.get(ctx.guild.id) and not ctx.voice_client.is_playing()):
        await ctx.respond("There are currently no songs in queue.")
        return
    
    cutoff = 5
    
    response = ""

    if ctx.voice_client.is_playing():
        if not CURRENT_SONG[ctx.guild.id]:
            await ctx.respond("Error, please try again.", ephemeral=True)
            return
        
        response += f"- **[{CURRENT_SONG[ctx.guild.id]['title']}](<{CURRENT_SONG[ctx.guild.id]['video_link']}>) - ({CURRENT_SONG[ctx.guild.id]['length']})**\n"

    for i in range(0, len(SONG_QUEUES[ctx.guild.id])):
        song = SONG_QUEUES[ctx.guild.id][i]

        if i == cutoff:
            response += f"- ...{len(SONG_QUEUES[ctx.guild.id]) - cutoff} more song(s)."
            break

        response += f"- [{song['title']}](<{song['video_link']}>) - ({song['length']})\n"
    
    await ctx.respond(response)

@bot.slash_command(
    name="clear_queue",
    description="Stop playback and clear entire queue",
    #guild_ids=[248493533537763328, 556956284159524981]
)
async def clear_queue(ctx: discord.ApplicationContext):
    if not ctx.voice_client or (not SONG_QUEUES.get(ctx.guild.id) and not ctx.voice_client.is_playing()):
        await ctx.respond("Queue already empty.")
        return
    
    SONG_QUEUES[ctx.guild.id].clear()

    if ctx.voice_client.is_playing():
        ctx.voice_client.stop()
    
    await ctx.respond("Stopped playback and cleared the queue.")

@bot.slash_command(
    name="skip",
    description="Skip the current song",
    #guild_ids=[248493533537763328, 556956284159524981]
    )
async def skip(ctx: discord.ApplicationContext):
    if ctx.voice_client and ctx.voice_client.is_playing():
        ctx.voice_client.stop()
        await ctx.respond("Song skipped.")
    else:
        await ctx.respond("No audio is currently playing.")

@bot.slash_command(
    name="pause",
    description="Pause the current playback",
    #guild_ids=[248493533537763328, 556956284159524981]
    )
async def pause(ctx: discord.ApplicationContext):
    if ctx.voice_client and ctx.voice_client.is_playing():
        ctx.voice_client.pause()
        await ctx.respond("Playback paused.")
    else:
        await ctx.respond("No audio is currently playing.")

@bot.slash_command(
    name="resume",
    description="Resume the current playback",
    #guild_ids=[248493533537763328, 556956284159524981]
    )
async def resume(ctx: discord.ApplicationContext):
    if ctx.voice_client and ctx.voice_client.is_paused():
        ctx.voice_client.resume()
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
        if guild_id in SONG_QUEUES:
            SONG_QUEUES[guild_id].clear()

@bot.listen(once=True)
async def on_ready():
    print('Logged in as', bot.user)

bot.run(config.DISCORD_TOKEN)