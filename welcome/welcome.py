from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, timezone
from enum import Enum
from typing import Literal, Optional, Union

import discord
from redbot.core import Config, commands
from redbot.core.bot import Red
from redbot.core.utils.chat_formatting import box, humanize_list, pagify

from .image_gen import PIL_AVAILABLE, generate_welcome_card

log = logging.getLogger("red.enigma.welcome")

_ON = "enabled"
_OFF = "disabled"

_EVENT_COLORS = {
    "join": 0x57F287,
    "leave": 0xFEE75C,
    "ban": 0xED4245,
    "unban": 0x5865F2,
}
_EVENT_EMOJIS = {
    "join": "👋",
    "leave": "🚪",
    "ban": "✈",
    "unban": "✅",
}


class WhisperType(Enum):
    OFF = "off"
    ONLY = "only"
    BOTH = "both"
    FALLBACK = "fall"


class Welcome(commands.Cog):
    """ProBot-style welcome notices with image cards, rich embeds, and full event coverage."""

    _GUILD_DEFAULTS = {
        "enabled": False,
        "channel": None,
        "join": {
            "enabled": True,
            "channel": None,
            "delete": False,
            "last": None,
            "whisper": {
                "state": "off",
                "message": "Hey there {member.name}, welcome to {server.name}!",
            },
            "messages": [
                "Welcome {member.mention} to **{server.name}**! You are member #{count}."
            ],
            "bot": None,
            "use_image": True,
            "bg_url": None,
            "accent_color": [88, 101, 242],
        },
        "leave": {
            "enabled": True,
            "channel": None,
            "delete": False,
            "last": None,
            "messages": ["**{member.name}** has left **{server.name}**."],
        },
        "ban": {
            "enabled": True,
            "channel": None,
            "delete": False,
            "last": None,
            "messages": ["**{member.name}** has been banned from **{server.name}**."],
        },
        "unban": {
            "enabled": True,
            "channel": None,
            "delete": False,
            "last": None,
            "messages": ["**{member.name}** has been unbanned from **{server.name}**."],
        },
        "joinlog": {
            "enabled": False,
            "channel": None,
        },
    }

    def __init__(self, bot: Red) -> None:
        super().__init__()
        self.bot = bot
        self.config = Config.get_conf(self, identifier=8934761200, force_registration=True)
        self.config.register_guild(**self._GUILD_DEFAULTS)

    async def red_delete_data_for_user(
        self,
        *,
        requester: Literal["discord_deleted_user", "owner", "user", "user_strict"],
        user_id: int,
    ) -> None:
        """Nothing to delete."""

    # ─── Main command ────────────────────────────────────────────────────────

    @commands.group(name="welcome", aliases=["welcomeset"])
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def welcome(self, ctx: commands.Context) -> None:
        """Manage welcome, leave, ban, and unban notices.

        Use `[p]welcomeset` as an alias for this command.
        """
        if ctx.invoked_subcommand is None:
            await self._show_settings(ctx)

    @welcome.command(name="toggle")
    async def welcome_toggle(self, ctx: commands.Context, on_off: Optional[bool] = None) -> None:
        """Turn the entire Welcome system on or off.

        Leave blank to flip the current state.
        """
        current = await self.config.guild(ctx.guild).enabled()
        state = on_off if on_off is not None else not current
        await self.config.guild(ctx.guild).enabled.set(state)
        await ctx.send(f"Welcome system is now **{_ON if state else _OFF}**.")

    @welcome.command(name="channel")
    async def welcome_channel(self, ctx: commands.Context, channel: discord.TextChannel) -> None:
        """Set the default channel used for all event notices."""
        if not self._can_speak_in(channel):
            await ctx.send(
                f"I don't have permission to send messages in {channel.mention}. "
                "Check my permissions and try again."
            )
            return
        await self.config.guild(ctx.guild).channel.set(channel.id)
        await ctx.send(f"Default event channel set to {channel.mention}.")

    # ─── Join ────────────────────────────────────────────────────────────────

    @welcome.group(name="join")
    async def welcome_join(self, ctx: commands.Context) -> None:
        """Change settings for join notices."""

    @welcome_join.command(name="toggle")
    async def wj_toggle(self, ctx: commands.Context, on_off: Optional[bool] = None) -> None:
        """Toggle join notices on or off."""
        await self._toggle_event(ctx, on_off, "join")

    @welcome_join.command(name="channel")
    async def wj_channel(
        self, ctx: commands.Context, channel: Optional[discord.TextChannel] = None
    ) -> None:
        """Set the join-specific channel. Leave blank to clear and fall back to the default."""
        await self._set_event_channel(ctx, channel, "join")

    @welcome_join.command(name="toggledelete")
    async def wj_toggledelete(self, ctx: commands.Context, on_off: Optional[bool] = None) -> None:
        """Toggle auto-deletion of the previous join notice."""
        await self._toggle_delete(ctx, on_off, "join")

    @welcome_join.group(name="whisper")
    async def wj_whisper(self, ctx: commands.Context) -> None:
        """Settings for DM-ing (whispering) new members on join."""

    @wj_whisper.command(name="type")
    async def wj_whisper_type(self, ctx: commands.Context, choice: WhisperType) -> None:
        """Set the DM behaviour when a member joins.

        Options:
          `off`  — no DM sent; only channel notice
          `only` — DM only; no channel notice
          `both` — DM the member **and** send a channel notice
          `fall` — DM if possible; if DM fails, send whisper message to channel
        """
        channel = await self._get_channel(ctx.guild, "join")
        await self.config.guild(ctx.guild).join.whisper.state.set(choice.value)
        ch_mention = channel.mention if channel else "the default channel"
        responses = {
            WhisperType.OFF: f"New members will not be DM'd. Channel notices go to {ch_mention}.",
            WhisperType.ONLY: "New members will only be DM'd. No channel notice will be sent.",
            WhisperType.BOTH: f"New members will be DM'd **and** a notice will go to {ch_mention}.",
            WhisperType.FALLBACK: (
                f"New members will be DM'd; if the DM fails the notice goes to {ch_mention}."
            ),
        }
        await ctx.send(responses[choice])

    @wj_whisper.command(name="message", aliases=["msg"])
    async def wj_whisper_message(self, ctx: commands.Context, *, msg_format: str) -> None:
        """Set the DM message sent to new members.

        Variables: `{member.name}` `{member.mention}` `{server.name}`
        """
        await self.config.guild(ctx.guild).join.whisper.message.set(msg_format)
        await ctx.send("Whisper message updated.")

    @welcome_join.group(name="message", aliases=["msg"])
    async def wj_message(self, ctx: commands.Context) -> None:
        """Manage the pool of join message formats. One is chosen at random each time."""

    @wj_message.command(name="add")
    async def wj_msg_add(self, ctx: commands.Context, *, msg_format: str) -> None:
        """Add a join message format to the random pool.

        Variables:
          `{member.mention}` `{member.name}` `{member.id}` `{member.discriminator}`
          `{server.name}` `{server.member_count}`
          `{count}` — total member count   `{roles}` — member's current roles
        """
        await self._add_message(ctx, msg_format, "join")

    @wj_message.command(name="delete", aliases=["del", "remove"])
    async def wj_msg_delete(self, ctx: commands.Context) -> None:
        """Remove a join message format from the pool."""
        await self._delete_message(ctx, "join")

    @wj_message.command(name="list", aliases=["ls"])
    async def wj_msg_list(self, ctx: commands.Context) -> None:
        """List all join message formats."""
        await self._list_messages(ctx, "join")

    @welcome_join.command(name="botmessage", aliases=["botmsg"])
    async def wj_botmsg(self, ctx: commands.Context, *, msg_format: Optional[str] = None) -> None:
        """Set a custom message format used when a bot joins. Leave blank to use normal join messages.

        Variables: `{bot.mention}` `{bot.name}` `{server.name}` `{count}`
        """
        await self.config.guild(ctx.guild).join.bot.set(msg_format)
        if msg_format:
            await ctx.send("Bot join message set.")
        else:
            await ctx.send("Bot join message cleared — bots will use normal join messages.")

    @welcome_join.group(name="image")
    async def wj_image(self, ctx: commands.Context) -> None:
        """Settings for the welcome image card sent on join."""

    @wj_image.command(name="toggle")
    async def wj_image_toggle(self, ctx: commands.Context, on_off: Optional[bool] = None) -> None:
        """Toggle the image card on or off for join notices."""
        current = await self.config.guild(ctx.guild).join.use_image()
        state = on_off if on_off is not None else not current
        await self.config.guild(ctx.guild).join.use_image.set(state)
        if state and not PIL_AVAILABLE:
            await ctx.send(
                f"Image cards are now **{_ON}**, but **Pillow is not installed** on this host. "
                "Run `pip install Pillow` to enable image generation."
            )
        else:
            await ctx.send(f"Welcome image card is now **{_ON if state else _OFF}**.")

    @wj_image.command(name="background", aliases=["bg"])
    async def wj_image_bg(self, ctx: commands.Context, url: Optional[str] = None) -> None:
        """Set a background image URL for the welcome card. Leave blank to use the default gradient."""
        await self.config.guild(ctx.guild).join.bg_url.set(url)
        if url:
            await ctx.send(f"Background image URL set.")
        else:
            await ctx.send("Background cleared — the default dark gradient will be used.")

    @wj_image.command(name="color", aliases=["colour"])
    async def wj_image_color(self, ctx: commands.Context, color: discord.Color) -> None:
        """Set the accent color for the welcome image card.

        Accepts hex (`#58a6ff`) or color names (`red`, `blurple`).
        """
        await self.config.guild(ctx.guild).join.accent_color.set([color.r, color.g, color.b])
        await ctx.send(f"Accent color set to `#{color.value:06x}`.")

    # ─── Leave ───────────────────────────────────────────────────────────────

    @welcome.group(name="leave")
    async def welcome_leave(self, ctx: commands.Context) -> None:
        """Change settings for leave notices."""

    @welcome_leave.command(name="toggle")
    async def wv_toggle(self, ctx: commands.Context, on_off: Optional[bool] = None) -> None:
        """Toggle leave notices on or off."""
        await self._toggle_event(ctx, on_off, "leave")

    @welcome_leave.command(name="channel")
    async def wv_channel(
        self, ctx: commands.Context, channel: Optional[discord.TextChannel] = None
    ) -> None:
        """Set the leave-specific channel. Leave blank to clear and use the default."""
        await self._set_event_channel(ctx, channel, "leave")

    @welcome_leave.command(name="toggledelete")
    async def wv_toggledelete(self, ctx: commands.Context, on_off: Optional[bool] = None) -> None:
        """Toggle auto-deletion of the previous leave notice."""
        await self._toggle_delete(ctx, on_off, "leave")

    @welcome_leave.group(name="message", aliases=["msg"])
    async def wv_message(self, ctx: commands.Context) -> None:
        """Manage the pool of leave message formats."""

    @wv_message.command(name="add")
    async def wv_msg_add(self, ctx: commands.Context, *, msg_format: str) -> None:
        """Add a leave message format.

        Variables: `{member.name}` `{member.id}` `{server.name}` `{server.member_count}` `{roles}`
        """
        await self._add_message(ctx, msg_format, "leave")

    @wv_message.command(name="delete", aliases=["del", "remove"])
    async def wv_msg_delete(self, ctx: commands.Context) -> None:
        """Remove a leave message format."""
        await self._delete_message(ctx, "leave")

    @wv_message.command(name="list", aliases=["ls"])
    async def wv_msg_list(self, ctx: commands.Context) -> None:
        """List all leave message formats."""
        await self._list_messages(ctx, "leave")

    # ─── Ban ─────────────────────────────────────────────────────────────────

    @welcome.group(name="ban")
    async def welcome_ban(self, ctx: commands.Context) -> None:
        """Change settings for ban notices."""

    @welcome_ban.command(name="toggle")
    async def wb_toggle(self, ctx: commands.Context, on_off: Optional[bool] = None) -> None:
        """Toggle ban notices on or off."""
        await self._toggle_event(ctx, on_off, "ban")

    @welcome_ban.command(name="channel")
    async def wb_channel(
        self, ctx: commands.Context, channel: Optional[discord.TextChannel] = None
    ) -> None:
        """Set the ban-specific channel. Leave blank to use the default."""
        await self._set_event_channel(ctx, channel, "ban")

    @welcome_ban.command(name="toggledelete")
    async def wb_toggledelete(self, ctx: commands.Context, on_off: Optional[bool] = None) -> None:
        """Toggle auto-deletion of the previous ban notice."""
        await self._toggle_delete(ctx, on_off, "ban")

    @welcome_ban.group(name="message", aliases=["msg"])
    async def wb_message(self, ctx: commands.Context) -> None:
        """Manage the pool of ban message formats."""

    @wb_message.command(name="add")
    async def wb_msg_add(self, ctx: commands.Context, *, msg_format: str) -> None:
        """Add a ban message format.

        Variables: `{member.name}` `{member.id}` `{server.name}`
        """
        await self._add_message(ctx, msg_format, "ban")

    @wb_message.command(name="delete", aliases=["del", "remove"])
    async def wb_msg_delete(self, ctx: commands.Context) -> None:
        """Remove a ban message format."""
        await self._delete_message(ctx, "ban")

    @wb_message.command(name="list", aliases=["ls"])
    async def wb_msg_list(self, ctx: commands.Context) -> None:
        """List all ban message formats."""
        await self._list_messages(ctx, "ban")

    # ─── Unban ───────────────────────────────────────────────────────────────

    @welcome.group(name="unban")
    async def welcome_unban(self, ctx: commands.Context) -> None:
        """Change settings for unban notices."""

    @welcome_unban.command(name="toggle")
    async def wu_toggle(self, ctx: commands.Context, on_off: Optional[bool] = None) -> None:
        """Toggle unban notices on or off."""
        await self._toggle_event(ctx, on_off, "unban")

    @welcome_unban.command(name="channel")
    async def wu_channel(
        self, ctx: commands.Context, channel: Optional[discord.TextChannel] = None
    ) -> None:
        """Set the unban-specific channel. Leave blank to use the default."""
        await self._set_event_channel(ctx, channel, "unban")

    @welcome_unban.command(name="toggledelete")
    async def wu_toggledelete(self, ctx: commands.Context, on_off: Optional[bool] = None) -> None:
        """Toggle auto-deletion of the previous unban notice."""
        await self._toggle_delete(ctx, on_off, "unban")

    @welcome_unban.group(name="message", aliases=["msg"])
    async def wu_message(self, ctx: commands.Context) -> None:
        """Manage the pool of unban message formats."""

    @wu_message.command(name="add")
    async def wu_msg_add(self, ctx: commands.Context, *, msg_format: str) -> None:
        """Add an unban message format.

        Variables: `{member.name}` `{member.id}` `{server.name}`
        """
        await self._add_message(ctx, msg_format, "unban")

    @wu_message.command(name="delete", aliases=["del", "remove"])
    async def wu_msg_delete(self, ctx: commands.Context) -> None:
        """Remove an unban message format."""
        await self._delete_message(ctx, "unban")

    @wu_message.command(name="list", aliases=["ls"])
    async def wu_msg_list(self, ctx: commands.Context) -> None:
        """List all unban message formats."""
        await self._list_messages(ctx, "unban")

    # ─── Join Log ────────────────────────────────────────────────────────────

    @welcome.group(name="joinlog")
    async def welcome_joinlog(self, ctx: commands.Context) -> None:
        """Settings for the join log — a compact embed showing account age sent to a staff channel."""

    @welcome_joinlog.command(name="toggle")
    async def wjl_toggle(self, ctx: commands.Context, on_off: Optional[bool] = None) -> None:
        """Toggle the join log on or off."""
        current = await self.config.guild(ctx.guild).joinlog.enabled()
        state = on_off if on_off is not None else not current
        await self.config.guild(ctx.guild).joinlog.enabled.set(state)
        await ctx.send(f"Join log is now **{_ON if state else _OFF}**.")

    @welcome_joinlog.command(name="channel")
    async def wjl_channel(
        self, ctx: commands.Context, channel: Optional[discord.TextChannel] = None
    ) -> None:
        """Set the channel for join log posts. Leave blank to clear."""
        if channel is not None and not self._can_speak_in(channel):
            await ctx.send(f"I don't have permission to send messages in {channel.mention}.")
            return
        await self.config.guild(ctx.guild).joinlog.channel.set(channel.id if channel else None)
        if channel:
            await ctx.send(f"Join log will be posted to {channel.mention}.")
        else:
            await ctx.send("Join log channel cleared.")

    # ─── Event Listeners ─────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        guild = member.guild
        cfg = self.config.guild(guild)

        if not await cfg.enabled() or not await cfg.join.enabled():
            return

        join_cfg = await cfg.join.all()

        # Bot-specific message
        if member.bot and join_cfg["bot"] is not None:
            await self._dispatch(guild, member, "join", message_format=join_cfg["bot"])
            return

        whisper_state = join_cfg["whisper"]["state"]

        if whisper_state == "off":
            await self._dispatch(guild, member, "join")
        else:
            dm_ok = await self._send_dm(member)
            if whisper_state == "only":
                pass
            elif whisper_state == "fall":
                if not dm_ok:
                    await self._dispatch(
                        guild,
                        member,
                        "join",
                        message_format=join_cfg["whisper"]["message"],
                        use_image=False,
                    )
            else:
                # "both" — DM sent, still send channel notice
                await self._dispatch(guild, member, "join")

        await self._send_joinlog(member)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        cfg = self.config.guild(member.guild)
        if not await cfg.enabled() or not await cfg.leave.enabled():
            return
        await self._dispatch(member.guild, member, "leave")

    @commands.Cog.listener()
    async def on_member_ban(
        self, guild: discord.Guild, member: Union[discord.Member, discord.User]
    ) -> None:
        cfg = self.config.guild(guild)
        if not await cfg.enabled() or not await cfg.ban.enabled():
            return
        moderator, reason = await self._fetch_audit(guild, member, discord.AuditLogAction.ban)
        await self._dispatch(guild, member, "ban", moderator=moderator, reason=reason)

    @commands.Cog.listener()
    async def on_member_unban(self, guild: discord.Guild, user: discord.User) -> None:
        cfg = self.config.guild(guild)
        if not await cfg.enabled() or not await cfg.unban.enabled():
            return
        moderator, reason = await self._fetch_audit(guild, user, discord.AuditLogAction.unban)
        await self._dispatch(guild, user, "unban", moderator=moderator, reason=reason)

    # ─── Private: dispatch ───────────────────────────────────────────────────

    async def _dispatch(
        self,
        guild: discord.Guild,
        user: Union[discord.Member, discord.User],
        event: str,
        *,
        message_format: Optional[str] = None,
        use_image: Optional[bool] = None,
        moderator: Optional[discord.Member] = None,
        reason: Optional[str] = None,
    ) -> None:
        cfg = self.config.guild(guild)
        event_cfg = await cfg.get_attr(event).all()

        # Delete previous notice if configured
        if event_cfg["delete"] and event_cfg["last"] is not None:
            await self._delete_last(guild, event_cfg["last"], event)
            await cfg.get_attr(event).last.set(None)

        channel = await self._get_channel(guild, event)
        if channel is None:
            log.warning("No sendable channel found for guild %s event %s", guild.id, event)
            return

        # Format the message text
        count = guild.member_count or 0
        roles_str = ""
        if isinstance(user, discord.Member):
            roles = [r.name for r in user.roles if r.name != "@everyone"]
            if roles:
                roles_str = humanize_list(roles)

        fmt = message_format or random.choice(event_cfg["messages"])
        try:
            text = fmt.format(
                member=user,
                server=guild,
                bot=user,
                count=count,
                plural="s" if count != 1 else "",
                roles=roles_str,
            )
        except (KeyError, AttributeError, IndexError):
            text = fmt

        sent: Optional[discord.Message] = None

        # Join → try image card first
        should_image = use_image if use_image is not None else event_cfg.get("use_image", True)
        if event == "join" and should_image:
            sent = await self._send_join_card(channel, user, guild, event_cfg, text, count)

        # Fallback (or non-join events) → rich embed
        if sent is None:
            sent = await self._send_embed(channel, user, event, text, moderator=moderator, reason=reason)

        if sent is not None:
            await cfg.get_attr(event).last.set(sent.id)

    async def _send_join_card(
        self,
        channel: discord.TextChannel,
        user: Union[discord.Member, discord.User],
        guild: discord.Guild,
        event_cfg: dict,
        text: str,
        count: int,
    ) -> Optional[discord.Message]:
        if not PIL_AVAILABLE:
            return None
        try:
            accent = tuple(event_cfg.get("accent_color", [88, 101, 242]))
            buf = await generate_welcome_card(
                avatar_url=str(user.display_avatar.replace(format="png", size=256)),
                username=str(user),
                display_name=getattr(user, "display_name", str(user)),
                server_name=guild.name,
                member_count=count,
                bg_url=event_cfg.get("bg_url"),
                accent_color=accent,
            )
        except Exception as exc:
            log.exception("Image card generation failed: %s", exc)
            return None

        if buf is None:
            return None

        try:
            embed = discord.Embed(description=text, color=discord.Color.from_rgb(*accent))
            embed.set_image(url="attachment://welcome.png")
            embed.set_footer(
                text=f"{guild.name} • {count:,} members",
                icon_url=guild.icon.url if guild.icon else None,
            )
            file = discord.File(buf, filename="welcome.png")
            return await channel.send(embed=embed, file=file)
        except discord.Forbidden:
            log.warning("Missing permissions to send to %s (%s)", channel.id, guild.id)
        except discord.HTTPException as exc:
            log.warning("HTTP error sending join card: %s", exc)
        return None

    async def _send_embed(
        self,
        channel: discord.TextChannel,
        user: Union[discord.Member, discord.User],
        event: str,
        text: str,
        *,
        moderator: Optional[discord.Member] = None,
        reason: Optional[str] = None,
    ) -> Optional[discord.Message]:
        guild = channel.guild
        emoji = _EVENT_EMOJIS.get(event, "")
        _event_desc = {
            "join": f"{user.mention} joined the server.",
            "leave": f"{user.mention} left.",
            "ban": f"{emoji} {user.mention} banned from the server.",
            "unban": f"{emoji} {user.mention} unbanned from the server.",
        }
        av_url = str(user.display_avatar.replace(format="png", size=256))

        embed = discord.Embed(
            description=_event_desc.get(event, text),
            color=_EVENT_COLORS.get(event, 0x808080),
            timestamp=discord.utils.utcnow(),
        )
        embed.set_author(name=str(user), icon_url=av_url)
        embed.set_thumbnail(url=av_url)

        if event in ("ban", "unban") and moderator is not None:
            embed.add_field(
                name="Responsible Moderator:",
                value=moderator.mention,
                inline=False,
            )
            if reason:
                embed.add_field(name="Reason:", value=reason, inline=False)
            embed.set_footer(
                text=str(moderator),
                icon_url=str(moderator.display_avatar.replace(format="png", size=64)),
            )
        else:
            if event in ("ban", "unban") and reason:
                embed.add_field(name="Reason:", value=reason, inline=False)
            embed.set_footer(
                text=guild.name,
                icon_url=guild.icon.url if guild.icon else None,
            )

        try:
            return await channel.send(embed=embed)
        except discord.Forbidden:
            log.warning("Missing permissions to send to %s", channel.id)
        except discord.HTTPException as exc:
            log.warning("HTTP error sending embed: %s", exc)
        return None

    async def _send_joinlog(self, member: discord.Member) -> None:
        guild = member.guild
        cfg = self.config.guild(guild)
        if not await cfg.joinlog.enabled():
            return

        ch_id = await cfg.joinlog.channel()
        channel = guild.get_channel(ch_id) if ch_id else None
        if channel is None or not self._can_speak_in(channel):
            return

        date_str, age_str = self._account_age(member.created_at)
        av_url = str(member.display_avatar.replace(format="png", size=256))

        embed = discord.Embed(
            description=f"{member.mention} joined the server.",
            color=0x57F287,
            timestamp=discord.utils.utcnow(),
        )
        embed.set_author(name=str(member), icon_url=av_url)
        embed.set_thumbnail(url=av_url)
        embed.add_field(
            name="\N{HEAVY CIRCLE} Age of account:",
            value=f"{date_str}\n{age_str}",
            inline=False,
        )
        embed.set_footer(
            text=guild.name,
            icon_url=guild.icon.url if guild.icon else None,
        )
        try:
            await channel.send(embed=embed)
        except (discord.Forbidden, discord.HTTPException) as exc:
            log.warning("Failed to send joinlog for guild %s: %s", guild.id, exc)

    async def _fetch_audit(
        self,
        guild: discord.Guild,
        target: Union[discord.Member, discord.User],
        action: discord.AuditLogAction,
    ) -> tuple:
        """Fetch the moderator and reason from the audit log for a ban/unban action."""
        await asyncio.sleep(0.5)
        try:
            async for entry in guild.audit_logs(limit=5, action=action):
                if entry.target.id == target.id:
                    return entry.user, entry.reason
        except discord.Forbidden:
            log.debug("Missing view_audit_log permission in guild %s", guild.id)
        except discord.HTTPException as exc:
            log.debug("Audit log fetch failed for guild %s: %s", guild.id, exc)
        return None, None

    @staticmethod
    def _account_age(created_at: datetime) -> tuple:
        date_str = created_at.strftime("%d/%m/%Y %H:%M")
        delta = datetime.now(timezone.utc) - created_at
        days = delta.days
        if days >= 365:
            n = days // 365
            age_str = f"{n} year{'s' if n != 1 else ''} ago"
        elif days >= 30:
            n = days // 30
            age_str = f"{n} month{'s' if n != 1 else ''} ago"
        elif days >= 1:
            age_str = f"{days} day{'s' if days != 1 else ''} ago"
        else:
            n = delta.seconds // 3600
            age_str = f"{n} hour{'s' if n != 1 else ''} ago" if n else "just now"
        return date_str, age_str

    async def _send_dm(self, member: discord.Member) -> bool:
        fmt = await self.config.guild(member.guild).join.whisper.message()
        try:
            text = fmt.format(member=member, server=member.guild)
        except (KeyError, AttributeError):
            text = fmt
        try:
            await member.send(text)
            return True
        except (discord.Forbidden, discord.HTTPException):
            return False

    # ─── Private: settings helpers ───────────────────────────────────────────

    async def _toggle_event(
        self, ctx: commands.Context, on_off: Optional[bool], event: str
    ) -> None:
        current = await self.config.guild(ctx.guild).get_attr(event).enabled()
        state = on_off if on_off is not None else not current
        await self.config.guild(ctx.guild).get_attr(event).enabled.set(state)
        await ctx.send(f"{event.capitalize()} notices are now **{_ON if state else _OFF}**.")

    async def _toggle_delete(
        self, ctx: commands.Context, on_off: Optional[bool], event: str
    ) -> None:
        current = await self.config.guild(ctx.guild).get_attr(event).delete()
        state = on_off if on_off is not None else not current
        await self.config.guild(ctx.guild).get_attr(event).delete.set(state)
        await ctx.send(
            f"Auto-deletion of previous {event} notice is now **{_ON if state else _OFF}**."
        )

    async def _set_event_channel(
        self,
        ctx: commands.Context,
        channel: Optional[discord.TextChannel],
        event: str,
    ) -> None:
        if channel is not None and not self._can_speak_in(channel):
            await ctx.send(f"I don't have permission to send messages in {channel.mention}.")
            return
        await self.config.guild(ctx.guild).get_attr(event).channel.set(
            channel.id if channel else None
        )
        if channel:
            await ctx.send(f"{event.capitalize()} notices will be sent to {channel.mention}.")
        else:
            default = await self._get_channel(ctx.guild, "default")
            mention = default.mention if default else "the default channel"
            await ctx.send(f"{event.capitalize()} channel cleared — using {mention}.")

    async def _add_message(
        self, ctx: commands.Context, msg_format: str, event: str
    ) -> None:
        async with self.config.guild(ctx.guild).get_attr(event).messages() as msgs:
            msgs.append(msg_format)
            total = len(msgs)
        await ctx.send(f"New {event} message format added. ({total} in pool)")

    async def _delete_message(self, ctx: commands.Context, event: str) -> None:
        messages = await self.config.guild(ctx.guild).get_attr(event).messages()
        if len(messages) <= 1:
            await ctx.send(
                f"There's only one {event} message format and it cannot be deleted."
            )
            return

        await self._list_messages(ctx, event)
        await ctx.send(
            f"Enter the **number** of the {event} message format to delete (15s timeout):"
        )

        def _check(m: discord.Message) -> bool:
            return (
                m.author == ctx.author
                and m.channel == ctx.channel
                and m.content.isdigit()
                and 1 <= int(m.content) <= len(messages)
            )

        try:
            reply = await ctx.bot.wait_for("message", check=_check, timeout=15.0)
        except asyncio.TimeoutError:
            await ctx.send("Timed out — no message deleted.")
            return

        idx = int(reply.content) - 1
        async with self.config.guild(ctx.guild).get_attr(event).messages() as msgs:
            removed = msgs.pop(idx)
        await ctx.send(f"Deleted format: `{removed}`")

    async def _list_messages(self, ctx: commands.Context, event: str) -> None:
        messages = await self.config.guild(ctx.guild).get_attr(event).messages()
        lines = "\n".join(f"{i}. {m}" for i, m in enumerate(messages, 1))
        for page in pagify(f"{event.capitalize()} message formats:\n{lines}", shorten_by=20):
            await ctx.send(box(page))

    # ─── Private: channel resolution ─────────────────────────────────────────

    async def _get_channel(
        self, guild: discord.Guild, event: str
    ) -> Optional[discord.TextChannel]:
        if event == "default":
            ch_id = await self.config.guild(guild).channel()
        else:
            ch_id = await self.config.guild(guild).get_attr(event).channel()

        ch = guild.get_channel(ch_id) if ch_id else None
        if ch and self._can_speak_in(ch):
            return ch

        # Fall back to the guild-level default
        fallback_id = await self.config.guild(guild).channel()
        ch = guild.get_channel(fallback_id) if fallback_id else None
        if ch and self._can_speak_in(ch):
            return ch

        # Fall back to Discord system channel
        if guild.system_channel and self._can_speak_in(guild.system_channel):
            return guild.system_channel

        # Last resort: first writable text channel
        for ch in guild.text_channels:
            if self._can_speak_in(ch):
                return ch

        return None

    async def _delete_last(self, guild: discord.Guild, message_id: int, event: str) -> None:
        channel = await self._get_channel(guild, event)
        if channel is None:
            return
        try:
            msg = await channel.fetch_message(message_id)
            await msg.delete()
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass

    # ─── Private: settings display ───────────────────────────────────────────

    async def _show_settings(self, ctx: commands.Context) -> None:
        guild = ctx.guild
        c = await self.config.guild(guild).all()

        ch = await self._get_channel(guild, "default")
        jch = await self._get_channel(guild, "join")
        vch = await self._get_channel(guild, "leave")
        bch = await self._get_channel(guild, "ban")
        uch = await self._get_channel(guild, "unban")

        jl = c["joinlog"]
        jl_ch = guild.get_channel(jl["channel"]) if jl["channel"] else None

        j, v, b, u = c["join"], c["leave"], c["ban"], c["unban"]
        jw = j["whisper"]
        whisper_prev = jw["message"][:50] + ("…" if len(jw["message"]) > 50 else "")

        def _ch(channel: Optional[discord.TextChannel]) -> str:
            return channel.mention if channel else "None"

        if await ctx.embed_requested():
            embed = discord.Embed(title="Current Welcome Settings", color=await ctx.embed_color())
            embed.add_field(
                name="General",
                inline=False,
                value=f"**Enabled:** {c['enabled']}\n**Channel:** {_ch(ch)}",
            )
            embed.add_field(
                name="Join",
                inline=False,
                value=(
                    f"**Enabled:** {j['enabled']}\n"
                    f"**Channel:** {_ch(jch)}\n"
                    f"**Delete previous:** {j['delete']}\n"
                    f"**Whisper state:** {jw['state']}\n"
                    f"**Whisper message:** {whisper_prev}\n"
                    f"**Messages:** {len(j['messages'])}; "
                    f"do `{ctx.prefix}welcomeset join msg list` for a list\n"
                    f"**Bot message:** {j['bot']}\n"
                    f"**Image card:** {'✅' if j['use_image'] else '❌'}"
                    + (f"\n**BG URL set:** yes" if j.get("bg_url") else "")
                ),
            )
            embed.add_field(
                name="Leave",
                inline=False,
                value=(
                    f"**Enabled:** {v['enabled']}\n"
                    f"**Channel:** {_ch(vch)}\n"
                    f"**Delete previous:** {v['delete']}\n"
                    f"**Messages:** {len(v['messages'])}; "
                    f"do `{ctx.prefix}welcomeset leave msg list` for a list"
                ),
            )
            embed.add_field(
                name="Ban",
                inline=False,
                value=(
                    f"**Enabled:** {b['enabled']}\n"
                    f"**Channel:** {_ch(bch)}\n"
                    f"**Delete previous:** {b['delete']}\n"
                    f"**Messages:** {len(b['messages'])}; "
                    f"do `{ctx.prefix}welcomeset ban msg list` for a list"
                ),
            )
            embed.add_field(
                name="Unban",
                inline=False,
                value=(
                    f"**Enabled:** {u['enabled']}\n"
                    f"**Channel:** {_ch(uch)}\n"
                    f"**Delete previous:** {u['delete']}\n"
                    f"**Messages:** {len(u['messages'])}; "
                    f"do `{ctx.prefix}welcomeset unban msg list` for a list"
                ),
            )
            embed.add_field(
                name="Join Log",
                inline=False,
                value=(
                    f"**Enabled:** {jl['enabled']}\n"
                    f"**Channel:** {jl_ch.mention if jl_ch else 'None'}"
                ),
            )
            await ctx.send(embed=embed)
        else:
            msg = (
                f"  Enabled: {c['enabled']}  |  Channel: {ch}\n\n"
                f"  Join:  {j['enabled']}  {jch}  del={j['delete']}  "
                f"whisper={jw['state']}  msgs={len(j['messages'])}  image={j['use_image']}\n"
                f"  Leave: {v['enabled']}  {vch}  del={v['delete']}  msgs={len(v['messages'])}\n"
                f"  Ban:   {b['enabled']}  {bch}  del={b['delete']}  msgs={len(b['messages'])}\n"
                f"  Unban: {u['enabled']}  {uch}  del={u['delete']}  msgs={len(u['messages'])}\n"
                f"  JoinLog: {jl['enabled']}  {jl_ch}\n"
            )
            await ctx.send(box(msg, "Current Welcome Settings"))

    @staticmethod
    def _can_speak_in(channel: discord.TextChannel) -> bool:
        return channel.permissions_for(channel.guild.me).send_messages
