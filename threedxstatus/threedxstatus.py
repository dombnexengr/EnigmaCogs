"""Red cog for monitoring the public 3DXChat service status endpoint."""

import asyncio
import logging
from typing import Any, Dict, Mapping, Optional, Tuple

import aiohttp
import discord
from discord.ext import tasks
from redbot.core import Config, checks, commands
from redbot.core.bot import Red

from .status import (
    SERVICE_KEYS,
    STATUS_API_URL,
    STATUS_PAGE_URL,
    classify_status,
    service_label,
    state_signature,
    status_description,
)

log = logging.getLogger("red.enigmacogs.threedxstatus")


class ThreeDXStatus(commands.Cog):
    """Show 3DXChat service health without posting repeated status messages."""

    POLL_SECONDS = 60.0
    API_FAILURES_BEFORE_UNKNOWN = 2
    CONFIG_IDENTIFIER = 90362304

    def __init__(self, bot: Red) -> None:
        self.bot = bot
        self.config = Config.get_conf(
            self,
            identifier=self.CONFIG_IDENTIFIER,
            force_registration=True,
        )
        self.config.register_global(presence_enabled=False)
        self.config.register_guild(
            enabled=False,
            status_channel_id=None,
            status_message_id=None,
            alerts_enabled=False,
            alerts_channel_id=None,
        )

        self._session: Optional[aiohttp.ClientSession] = None
        self._last_status: Optional[str] = None
        self._last_signature: Optional[str] = None
        self._api_failures = 0

    async def cog_load(self) -> None:
        """Start the single polling loop used by this Red instance."""
        self.update_loop.start()

    def cog_unload(self) -> None:
        """Stop polling and close the HTTP session when the cog is unloaded."""
        self.update_loop.cancel()
        if self._session is not None and not self._session.closed:
            self.bot.loop.create_task(self._session.close())

    async def _http_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=10)
            self._session = aiohttp.ClientSession(
                timeout=timeout,
                headers={
                    "Accept": "application/json",
                    "User-Agent": "EnigmaCogs-threedxstatus/1.0",
                },
            )
        return self._session

    async def _fetch_status(self) -> Mapping[str, Any]:
        session = await self._http_session()
        async with session.get(STATUS_API_URL) as response:
            if response.status != 200:
                raise RuntimeError("3DXChat status API returned HTTP %s" % response.status)
            payload = await response.json(content_type=None)
            if not isinstance(payload, Mapping):
                raise ValueError("3DXChat status API returned an invalid JSON object")
            return payload

    async def _has_destination(self) -> bool:
        if await self.config.presence_enabled():
            return True
        guilds = await self.config.all_guilds()
        return any(settings.get("enabled") for settings in guilds.values())

    @tasks.loop(seconds=POLL_SECONDS)
    async def update_loop(self) -> None:
        """Poll once and update all configured guilds from the same result."""
        if not await self._has_destination():
            return
        await self._poll_and_publish()

    @update_loop.before_loop
    async def before_update_loop(self) -> None:
        await self.bot.wait_until_ready()

    async def _poll_and_publish(
        self,
        suppress_alerts: bool = False,
        force: bool = False,
    ) -> str:
        try:
            payload = await self._fetch_status()
        except (aiohttp.ClientError, asyncio.TimeoutError, RuntimeError, ValueError) as exc:
            self._api_failures += 1
            log.warning("Unable to read the 3DXChat status endpoint: %s", exc)
            if self._api_failures < self.API_FAILURES_BEFORE_UNKNOWN:
                return self._last_status or "UNKNOWN"
            return await self._publish(
                payload=None,
                status="UNKNOWN",
                error="The status endpoint could not be reached.",
                suppress_alerts=suppress_alerts,
                force=force,
            )

        self._api_failures = 0
        status = classify_status(payload)
        return await self._publish(
            payload=payload,
            status=status,
            error=None,
            suppress_alerts=suppress_alerts,
            force=force,
        )

    async def _publish(
        self,
        payload: Optional[Mapping[str, Any]],
        status: str,
        error: Optional[str],
        suppress_alerts: bool,
        force: bool,
    ) -> str:
        previous_status = self._last_status
        signature = state_signature(payload, status)
        status_changed = previous_status is not None and status != previous_status

        self._last_status = status

        if await self.config.presence_enabled() and (status_changed or self._last_signature is None):
            await self._set_presence(status)

        if signature == self._last_signature and not status_changed and not force:
            return status

        embed = self._build_status_embed(payload, status, error)
        guild_settings = await self.config.all_guilds()

        for guild_id, settings in guild_settings.items():
            if not settings.get("enabled"):
                continue

            guild = self.bot.get_guild(int(guild_id))
            if guild is None:
                continue

            await self._update_status_message(guild, settings, embed)

            if (
                not suppress_alerts
                and status_changed
                and settings.get("alerts_enabled")
                and self._should_alert(previous_status, status)
            ):
                await self._send_alert(guild, settings, previous_status, status, embed)

        self._last_signature = signature
        return status

    @staticmethod
    def _should_alert(previous_status: Optional[str], status: str) -> bool:
        if previous_status is None or status == "UNKNOWN":
            return False
        if previous_status == "UNKNOWN":
            return False
        return previous_status != status

    async def _set_presence(self, status: str) -> None:
        status_map = {
            "ONLINE": discord.Status.online,
            "ISSUES": discord.Status.idle,
            "MAINTENANCE": discord.Status.idle,
            "OFFLINE": discord.Status.dnd,
            "UNKNOWN": discord.Status.idle,
        }
        try:
            await self.bot.change_presence(
                status=status_map.get(status, discord.Status.idle),
                activity=discord.Activity(
                    type=discord.ActivityType.watching,
                    name="3DXChat: %s" % status,
                    url=STATUS_PAGE_URL,
                ),
            )
        except discord.HTTPException:
            log.exception("Unable to update the Red bot presence")

    async def _resolve_channel(self, channel_id: Optional[int]) -> Optional[discord.abc.Messageable]:
        if not channel_id:
            return None

        channel = self.bot.get_channel(int(channel_id))
        if channel is not None:
            return channel

        try:
            channel = await self.bot.fetch_channel(int(channel_id))
        except (discord.Forbidden, discord.HTTPException, discord.NotFound):
            log.warning("Unable to access configured Discord channel %s", channel_id)
            return None
        return channel

    async def _update_status_message(
        self,
        guild: discord.Guild,
        settings: Mapping[str, Any],
        embed: discord.Embed,
    ) -> None:
        channel = await self._resolve_channel(settings.get("status_channel_id"))
        if channel is None or not hasattr(channel, "send"):
            return

        message = None
        message_id = settings.get("status_message_id")
        if message_id and hasattr(channel, "fetch_message"):
            try:
                message = await channel.fetch_message(int(message_id))
            except discord.NotFound:
                message = None
            except (discord.Forbidden, discord.HTTPException):
                log.warning("Unable to read the 3DXChat status message in %s", guild.name)
                return

        try:
            if message is None:
                message = await channel.send(
                    embed=embed,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                await self.config.guild(guild).status_message_id.set(message.id)
            else:
                await message.edit(embed=embed)
        except (discord.Forbidden, discord.HTTPException):
            log.warning("Unable to update the 3DXChat status message in %s", guild.name)

    async def _send_alert(
        self,
        guild: discord.Guild,
        settings: Mapping[str, Any],
        previous_status: Optional[str],
        status: str,
        status_embed: discord.Embed,
    ) -> None:
        channel_id = settings.get("alerts_channel_id") or settings.get("status_channel_id")
        channel = await self._resolve_channel(channel_id)
        if channel is None or not hasattr(channel, "send"):
            return

        if status == "ONLINE" and previous_status in {"OFFLINE", "ISSUES", "MAINTENANCE"}:
            title = "3DXChat service restored"
            description = "The monitored services have returned to an operational state."
            color = discord.Color.green()
        else:
            title = "3DXChat status changed: %s" % status
            description = status_description(status)
            color = status_embed.color or discord.Color.orange()

        embed = discord.Embed(
            title=title,
            url=STATUS_PAGE_URL,
            description=description,
            color=color,
        )
        embed.add_field(name="Previous status", value=previous_status or "Unknown", inline=True)
        embed.add_field(name="Current status", value=status, inline=True)
        embed.set_footer(text="Alerts are sent only when the monitored state changes.")

        try:
            await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        except (discord.Forbidden, discord.HTTPException):
            log.warning("Unable to send a 3DXChat status alert in %s", guild.name)

    @staticmethod
    def _service_value(service: Mapping[str, Any]) -> str:
        if service.get("isUp") is False:
            return "🔴 OFFLINE"
        recent_failures = service.get("recentChecksFailed")
        has_recent_failures = (
            isinstance(recent_failures, (int, float))
            and not isinstance(recent_failures, bool)
            and recent_failures > 0
        )
        if service.get("previousCheckFailed") is True or has_recent_failures:
            return "🟠 POSSIBLE ISSUES"
        if service.get("isUp") is True:
            latency = service.get("latency")
            if isinstance(latency, (int, float)) and not isinstance(latency, bool):
                return "🟢 ONLINE · %d ms" % round(latency)
            return "🟢 ONLINE"
        return "⚪ UNKNOWN"

    def _build_status_embed(
        self,
        payload: Optional[Mapping[str, Any]],
        status: str,
        error: Optional[str],
    ) -> discord.Embed:
        color_map = {
            "ONLINE": discord.Color.green(),
            "ISSUES": discord.Color.orange(),
            "MAINTENANCE": discord.Color.orange(),
            "OFFLINE": discord.Color.red(),
            "UNKNOWN": discord.Color.light_grey(),
        }
        embed = discord.Embed(
            title="3DXChat Service Status",
            url=STATUS_PAGE_URL,
            description=status_description(status),
            color=color_map.get(status, discord.Color.light_grey()),
        )
        embed.add_field(name="Overall", value=status, inline=False)

        if payload is not None:
            for service_name in SERVICE_KEYS:
                service = payload.get(service_name)
                if isinstance(service, Mapping):
                    value = self._service_value(service)
                else:
                    value = "⚪ UNKNOWN"
                embed.add_field(name=service_label(service_name), value=value, inline=True)

            uptimeday = payload.get("uptimeday")
            uptimeweek = payload.get("uptimeweek")
            if isinstance(uptimeday, Mapping) and isinstance(uptimeweek, Mapping):
                uptime_value = "24 hours: %s%%\n7 days: %s%%" % (
                    uptimeday.get("uptimePerc", "—"),
                    uptimeweek.get("uptimePerc", "—"),
                )
                embed.add_field(name="Uptime", value=uptime_value, inline=True)

            latest_patch = payload.get("latestpatch")
            if isinstance(latest_patch, Mapping) and latest_patch.get("version") is not None:
                embed.add_field(
                    name="Latest build",
                    value=str(latest_patch["version"]),
                    inline=True,
                )

            down_services = payload.get("downServices")
            if isinstance(down_services, (list, tuple)) and down_services:
                embed.add_field(
                    name="Reported down services",
                    value=", ".join(str(service) for service in down_services)[:1024],
                    inline=False,
                )

            message = payload.get("message")
            if message:
                embed.add_field(name="Notice", value=str(message)[:1024], inline=False)

            last_update = payload.get("lastupdate")
            if isinstance(last_update, (int, float)) and not isinstance(last_update, bool):
                embed.add_field(
                    name="Monitor update",
                    value="<t:%d:R>" % int(last_update),
                    inline=True,
                )
        elif error:
            embed.add_field(name="Details", value=error, inline=False)

        embed.set_footer(text="Source: status.3dxchat.net · Refresh interval: 60 seconds")
        return embed

    @commands.group(name="3dxstatus", invoke_without_command=True)
    @commands.guild_only()
    async def three_dx_status(self, ctx: commands.Context) -> None:
        """Show the current 3DXChat service status."""
        if ctx.invoked_subcommand is not None:
            return

        try:
            payload = await self._fetch_status()
            status = classify_status(payload)
            embed = self._build_status_embed(payload, status, None)
        except (aiohttp.ClientError, asyncio.TimeoutError, RuntimeError, ValueError):
            embed = self._build_status_embed(
                payload=None,
                status="UNKNOWN",
                error="The status endpoint could not be reached.",
            )
        await ctx.send(embed=embed)

    @three_dx_status.command(name="setup")
    @checks.admin_or_permissions(manage_guild=True)
    async def setup_status(
        self,
        ctx: commands.Context,
        channel: Optional[discord.TextChannel] = None,
    ) -> None:
        """Create or reuse one editable status message in a channel."""
        target = channel or ctx.channel
        if not isinstance(target, discord.TextChannel) or target.guild != ctx.guild:
            await ctx.send("Please choose a text channel from this server.", delete_after=10)
            return

        guild_config = self.config.guild(ctx.guild)
        await guild_config.status_channel_id.set(target.id)
        await guild_config.enabled.set(True)
        await self._poll_and_publish(suppress_alerts=True, force=True)
        await ctx.send(
            "3DXChat status monitoring is enabled in %s. The cog will edit one message instead "
            "of posting repeated updates." % target.mention,
            delete_after=15,
        )

    @three_dx_status.command(name="disable")
    @checks.admin_or_permissions(manage_guild=True)
    async def disable_status(self, ctx: commands.Context) -> None:
        """Stop updating the configured status message for this server."""
        await self.config.guild(ctx.guild).enabled.set(False)
        await ctx.send("3DXChat status monitoring is disabled for this server.", delete_after=10)

    @three_dx_status.command(name="alerts")
    @checks.admin_or_permissions(manage_guild=True)
    async def alerts(self, ctx: commands.Context, enabled: bool) -> None:
        """Enable or disable one-message-per-state-change alerts."""
        await self.config.guild(ctx.guild).alerts_enabled.set(enabled)
        state = "enabled" if enabled else "disabled"
        await ctx.send("3DXChat status alerts are %s." % state, delete_after=10)

    @three_dx_status.command(name="alertchannel")
    @checks.admin_or_permissions(manage_guild=True)
    async def alert_channel(
        self,
        ctx: commands.Context,
        channel: Optional[discord.TextChannel] = None,
    ) -> None:
        """Set a separate alert channel, or return alerts to the status channel."""
        channel_id = channel.id if channel is not None and channel.guild == ctx.guild else None
        await self.config.guild(ctx.guild).alerts_channel_id.set(channel_id)
        if channel_id is None:
            await ctx.send("Alerts will use the configured status channel.", delete_after=10)
        else:
            await ctx.send("Alerts will be sent to %s." % channel.mention, delete_after=10)

    @three_dx_status.command(name="presence")
    @checks.is_owner()
    async def presence(self, ctx: commands.Context, enabled: bool) -> None:
        """Enable or disable the global Red bot presence status."""
        await self.config.presence_enabled.set(enabled)
        if enabled and self._last_status is not None:
            await self._set_presence(self._last_status)
        state = "enabled" if enabled else "disabled"
        await ctx.send("Global 3DXChat presence is %s." % state, delete_after=10)

    @three_dx_status.command(name="now")
    async def now(self, ctx: commands.Context) -> None:
        """Force a fresh status read and display it in the current channel."""
        try:
            payload = await self._fetch_status()
            status = classify_status(payload)
            embed = self._build_status_embed(payload, status, None)
        except (aiohttp.ClientError, asyncio.TimeoutError, RuntimeError, ValueError):
            embed = self._build_status_embed(
                payload=None,
                status="UNKNOWN",
                error="The status endpoint could not be reached.",
            )
        await ctx.send(embed=embed)
