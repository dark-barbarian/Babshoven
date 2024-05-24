import config
import discord
import asyncio
import yt_dlp as youtube_dl
import os

from discord.ext import commands

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
async def ping(ctx):
    await ctx.respond(embed=create_embed('Latency', f'{round(bot.latency * 1000)} ms', color=0x000000))
    return

##################################################################
######################### MUSIC METHODS ##########################
##################################################################

@bot.slash_command(name="join", description="Join the voice channel", guild_ids=[248493533537763328])
async def join(ctx: discord.ApplicationContext):
    if ctx.author.voice:
        channel = ctx.author.voice.channel
        await channel.connect()
        await ctx.respond(f"Joined {channel}")
    else:
        await ctx.respond("You are not in a voice channel!")
 
@bot.slash_command(name="leave", description="Leave the voice channel", guild_ids=[248493533537763328])
async def leave(ctx: discord.ApplicationContext):
    if ctx.voice_client:
        await ctx.voice_client.disconnect()
        await ctx.respond("Left the voice channel.")
    else:
        await ctx.respond("I am not in a voice channel!")
 
@bot.slash_command(name="play", description="Play a YouTube video", guild_ids=[248493533537763328, 556956284159524981])
async def play(ctx: discord.ApplicationContext, url: str):
    if not ctx.voice_client:
        if ctx.author.voice:
            channel = ctx.author.voice.channel
            await channel.connect()
        else:
            await ctx.respond("You are not in a voice channel!")
            return

    await ctx.defer()  

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
        info_dict = ydl.extract_info(url, download=True)
        filename = ydl.prepare_filename(info_dict)
        mp3_filename = filename.rsplit('.', 1)[0] + '.mp3'

        if not os.path.isfile(mp3_filename):
            os.rename(filename, mp3_filename)

        source = discord.FFmpegPCMAudio(mp3_filename)

    ctx.voice_client.play(source, after=lambda e: print(f'Finished playing: {e}'))
    await ctx.respond(f"Now playing: {info_dict['title']}")

@bot.slash_command(name="stop", description="Stop the current playback", guild_ids=[248493533537763328, 556956284159524981])
async def stop(ctx: discord.ApplicationContext):
    if ctx.voice_client and ctx.voice_client.is_playing():
        ctx.voice_client.stop()
        await ctx.respond("Playback stopped.")
    else:
        await ctx.respond("No audio is currently playing.")

@bot.slash_command(name="pause", description="Pause the current playback", guild_ids=[248493533537763328, 556956284159524981])
async def pause(ctx: discord.ApplicationContext):
    if ctx.voice_client and ctx.voice_client.is_playing():
        ctx.voice_client.pause()
        await ctx.respond("Playback paused.")
    else:
        await ctx.respond("No audio is currently playing.")

@bot.slash_command(name="resume", description="Resume the current playback", guild_ids=[248493533537763328, 556956284159524981])
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