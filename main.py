import re
import config
import discord
import asyncio
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

BOT_VOLUME = 0.2

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
    return

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
    global BOT_VOLUME
    BOT_VOLUME = float(volume) / 100
    print(BOT_VOLUME)
    await ctx.respond(f"Changed the volume to {volume}.")

@bot.slash_command(
    name="play",
    description="Play a YouTube video",
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
    if bool(url) == bool(search_terms):
        await ctx.respond("You need to provide either an URL or search terms.", ephemeral=True)
        return

    if not ctx.voice_client:
        if ctx.author.voice:
            channel = ctx.author.voice.channel
            await channel.connect()
            await ctx.send("Joined voice channel!")
        else:
            await ctx.respond("You are not in a voice channel!")
            return
    
    if url and re.search("^(?:https?:\/\/(?:www\.)?)?(?:(?:youtube\.com)|(?:youtu\.be))", url) is None:
        await ctx.respond("Currently, only YouTube is supported.", ephemeral=True)
        return 

    await ctx.defer(ephemeral=True)

    ydl_opts = {
        'format': 'bestaudio/best',
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '192',
        }],
        'outtmpl': 'downloads/%(title)s.%(ext)s',
    }

    if not os.path.exists('downloads'):
        os.makedirs('downloads')

    with youtube_dl.YoutubeDL(ydl_opts) as ydl:
        info_dict = ydl.extract_info(url or f"ytsearch:{search_terms}", download=False)
        
        if (url and info_dict['duration'] > 600) or (search_terms and info_dict['entries'][0]['duration'] > 600):
            await ctx.respond("Video must be shorter than 10 minutes.")
            return

        info_dict = ydl.extract_info(url or f"ytsearch:{search_terms}", download=True)
        filename = (url and ydl.prepare_filename(info_dict)) or ydl.prepare_filename(info_dict['entries'][0])
        mp3_filename = filename.rsplit('.', 1)[0] + '.mp3'

        if not os.path.isfile(mp3_filename):
            os.rename(filename, mp3_filename)

        source = discord.FFmpegPCMAudio(mp3_filename)
        source = discord.PCMVolumeTransformer(source, BOT_VOLUME)

    ctx.voice_client.play(source, after=lambda e: print(f'Finished playing: {e}'))
    await ctx.respond("Success! Your song is about to be played!")
    await ctx.send(f"Now playing: **{url and info_dict['title'] or info_dict['entries'][0]['title']}** - ({url and info_dict['duration_string'] or info_dict['entries'][0]['duration_string']})")

@bot.slash_command(
        name="stop",
        description="Stop the current playback",
        #guild_ids=[248493533537763328, 556956284159524981]
        )
async def stop(ctx: discord.ApplicationContext):
    if ctx.voice_client and ctx.voice_client.is_playing():
        ctx.voice_client.stop()
        await ctx.respond("Playback stopped.")
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

@bot.listen(once=True)
async def on_ready():
    print('Logged in as', bot.user)

bot.run(config.DISCORD_TOKEN)


## TODO: volume slider, queue, auto-delete of files, search function, song info