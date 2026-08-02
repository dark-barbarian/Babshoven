import os
import sys
from typing import cast

import discord
from discord.ext import commands

from cogs.songs import Songs
from utils.bot import Bot


class General(commands.Cog):
    """Cog for maintenance commands."""

    def __init__(self, bot: Bot) -> None:
        self.bot = bot

    @staticmethod
    def update_playlist_limit_option(bot: Bot) -> None:
        """Synchronize the slash-command option metadata with the current playlist limit."""
        for cmd_option in cast("discord.SlashCommand", Songs.play).options:
            if cmd_option.name != "playlist_limit":
                continue
            cmd_option.description = cmd_option.description.rsplit(" ", 1)[0] + " " + str(bot.playlist_songs_limit)
            cmd_option.max_value = bot.playlist_songs_limit
            break

    @commands.slash_command(name="ping", description="Check the bot's latency")
    async def ping(self, ctx: discord.ApplicationContext) -> None:
        """Check the bot's latency and respond with the ping time."""
        await ctx.respond(f"Latency: {round(self.bot.latency * 1000)} ms")

    @commands.slash_command(name="restart", description="Restart the bot")
    @commands.check_any(commands.is_owner(), Bot.is_one_of_the_bois())  # pyright: ignore[reportArgumentType]
    async def restart(self, ctx: discord.ApplicationContext) -> None:
        """Restart the bot process with the same command-line arguments."""
        interaction = await ctx.respond("Restarting...")
        response = await cast("discord.Interaction", interaction).original_response()
        os.execv(  # noqa: S606
            sys.executable,
            ["python", *sys.argv, str(response.channel.id), str(response.id)],
        )

    @commands.slash_command(name="clear_cache", description="Clear download cache")
    @commands.check_any(commands.is_owner(), Bot.is_one_of_the_bois())  # pyright: ignore[reportArgumentType]
    async def clear_cache(self, ctx: discord.ApplicationContext) -> None:
        """Clear the download archive cache."""
        self.bot.download_archive.clear()
        await ctx.respond("Cleared download cache.")

    @commands.slash_command(name="override_limits", description="Override the bot's limits")
    @discord.option(
        "max_song_length",
        description="Maximum song length in minutes",
        required=False,
        input_type=int,
    )
    @discord.option(
        "playlist_limit",
        description="Maximum number of songs in a playlist",
        required=False,
        input_type=int,
    )
    @commands.is_owner()
    async def override_limits(
        self,
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
            self.bot.song_max_length_minutes = max_song_length
            await ctx.respond(
                f"Changed maximum song duration from {self.bot.song_max_length_minutes} to {max_song_length}!"
            )

        if playlist_limit:
            self.bot.playlist_songs_limit = playlist_limit
            await ctx.respond(
                "Changed maximum number of songs per playlist from"
                f" {self.bot.playlist_songs_limit} to {playlist_limit}!"
            )

        self.update_playlist_limit_option(self.bot)

        await self.bot.sync_commands()

    @commands.slash_command(name="leave", description="Leave the voice channel")
    @commands.guild_only()
    async def leave(self, ctx: discord.ApplicationContext) -> None:
        """Leave the voice channel."""
        if ctx.voice_client and ctx.voice_client.is_connected():
            await ctx.voice_client.disconnect()
            await ctx.respond("Left the voice channel.")
        else:
            await ctx.respond("I am not in a voice channel!", ephemeral=True)


def setup(bot: Bot) -> None:
    """Register the `General` cog with the bot."""
    bot.add_cog(General(bot))
