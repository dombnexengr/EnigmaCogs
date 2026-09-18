"""Day-based DJ slot scheduling for Red-DiscordBot."""

import asyncio
import logging
from collections import defaultdict
from datetime import date as Date, datetime, timezone
from typing import List, Optional, Tuple

import discord
from redbot.core import Config, commands
from redbot.core.bot import Red

from .helpers import (
    DISPLAY_ZONES,
    ZoneInfo,
    build_slots,
    parse_day,
    parse_hour,
    resolve_tz,
    summer_note,
    zone_line,
)

MAX_PER_HOUR = 10
log = logging.getLogger("red.EnigmaCogs.djscheduler")


class DJScheduler(commands.Cog):
    """Pick a day, set how many slots each hour has, and let DJs claim the hour they want.

    Every slot is shown in Eastern, Central European and Pacific time at once.
    """

    __author__ = "dombn"
    __version__ = "1.0.1"

    def __init__(self, bot: Red) -> None:
        self.bot = bot
        self._board_locks = defaultdict(asyncio.Lock)
        self.config = Config.get_conf(self, identifier=7412583690, force_registration=True)
        self.config.register_guild(
            anchor_tz="America/New_York",
            board_channel=None,
            announce_channel=None,
            announce=True,
            dj_role=None,
            schedules={},
        )

    def format_help_for_context(self, ctx: commands.Context) -> str:
        return f"{super().format_help_for_context(ctx)}\n\nVersion: {self.__version__}"

    async def red_delete_data_for_user(self, *, requester, user_id: int) -> None:
        for guild_id in await self.config.all_guilds():
            changed = []
            async with self.config.guild_from_id(guild_id).schedules() as schedules:
                for key, schedule in schedules.items():
                    for slot in schedule["slots"].values():
                        if slot["user"] == user_id:
                            slot["user"] = None
                            if key not in changed:
                                changed.append(key)
            guild = self.bot.get_guild(guild_id)
            if guild:
                for key in changed:
                    await self._refresh(guild, key)

    # ------------------------------------------------------------------ #
    # internals
    # ------------------------------------------------------------------ #

    @staticmethod
    def _next_id(schedules: dict) -> int:
        """Next slot number, reserving IDs in both open and closed schedules."""
        used = [int(slot_id) for schedule in schedules.values() for slot_id in schedule["slots"]]
        return max(used) + 1 if used else 1

    @staticmethod
    def _locate(schedules: dict, slot_id: int, open_only: bool = True):
        """Find the schedule a slot number belongs to, preferring open days.

        Legacy data may reuse IDs on closed days, so prefer an open match.
        """
        fallback = None
        for key, schedule in schedules.items():
            if str(slot_id) not in schedule["slots"]:
                continue
            if schedule.get("open", True):
                return key, schedule
            if not open_only and fallback is None:
                fallback = (key, schedule)
        return fallback

    @staticmethod
    def _missing(schedules: dict, slot_id: int) -> str:
        """Explain why a slot number is not claimable."""
        for key, schedule in schedules.items():
            if str(slot_id) in schedule["slots"]:
                return f"Signups for **{key}** are closed, so slot **#{slot_id}** is locked."
        return f"There is no slot **#{slot_id}**."

    @staticmethod
    def _held_by(schedule: dict, user_id: int) -> List[int]:
        return sorted(
            int(slot_id) for slot_id, slot in schedule["slots"].items() if slot["user"] == user_id
        )

    async def _embeds(self, guild: discord.Guild, key: str) -> List[discord.Embed]:
        schedules = await self.config.guild(guild).schedules()
        schedule = schedules.get(key)
        if schedule is None:
            return []
        day = Date.fromisoformat(key)
        slots = schedule["slots"]
        is_open = schedule.get("open", True)

        blocks: List[str] = []
        taken = 0
        for ts in sorted({slot["ts"] for slot in slots.values()}):
            ids = sorted(int(i) for i, slot in slots.items() if slot["ts"] == ts)
            entries = []
            for slot_id in ids:
                user_id = slots[str(slot_id)]["user"]
                if user_id:
                    taken += 1
                    member = guild.get_member(user_id)
                    who = member.mention if member else f"<@{user_id}>"
                else:
                    who = "*free*"
                entries.append(f"`#{slot_id}` {who}")
            blocks.append(f"**{zone_line(ts, day)}**\n{' · '.join(entries)}")

        prefixes = await self.bot.get_valid_prefixes(guild)
        prefix = next((p for p in prefixes if not p.startswith("<@")), prefixes[0])
        if is_open:
            header = (
                f"Claim a free slot with `{prefix}dj claim <number>` — one slot per DJ per day.\n"
            )
        else:
            header = "\U0001f512 **Signups are closed for this day.**\n"

        # Each page is a separate message: Discord's 6000-character embed
        # budget applies to all embeds in a single message combined.
        pages = []
        current = header
        for block in blocks:
            if len(current) + len(block) + 2 > 3900:
                pages.append(current)
                current = header
            current += f"\n\n{block}"
        pages.append(current)
        notes = sorted({note for slot in slots.values() if (note := summer_note(slot["ts"]))})
        embeds = []
        for number, body in enumerate(pages, start=1):
            embed = discord.Embed(
                title=f"\U0001f3a7 DJ Schedule — {day:%A, %d %B %Y}",
                description=body,
                colour=discord.Colour.blurple() if is_open else discord.Colour.dark_grey(),
            )
            if notes:
                embed.add_field(name="Daylight saving", value="\n".join(notes), inline=False)
            embed.set_footer(text=f"{taken}/{len(slots)} slots taken · Page {number}/{len(pages)}")
            embeds.append(embed)
        return embeds

    @staticmethod
    def _message_ids(schedule: dict) -> List[int]:
        """Read new multi-page boards and boards saved by version 1.0.0."""
        return schedule.get("message_ids") or (
            [schedule["message_id"]] if schedule.get("message_id") else []
        )

    async def _refresh(self, guild: discord.Guild, key: str) -> None:
        """Re-render the posted board for a day, if there is one."""
        async with self._board_locks[guild.id]:
            schedules = await self.config.guild(guild).schedules()
            schedule = schedules.get(key)
            if not schedule or not self._message_ids(schedule):
                return
            channel = guild.get_channel(schedule.get("channel_id") or 0)
            if not isinstance(channel, discord.TextChannel):
                return
            ids = list(self._message_ids(schedule))
            embeds = await self._embeds(guild, key)
            try:
                for index, embed in enumerate(embeds):
                    message = None
                    if index < len(ids):
                        try:
                            message = await channel.fetch_message(ids[index])
                            await message.edit(
                                embed=embed, allowed_mentions=discord.AllowedMentions.none()
                            )
                        except discord.NotFound:
                            message = None
                    if message is None:
                        message = await channel.send(
                            embed=embed, allowed_mentions=discord.AllowedMentions.none()
                        )
                    if index < len(ids):
                        ids[index] = message.id
                    else:
                        ids.append(message.id)
                while len(ids) > len(embeds):
                    try:
                        message = await channel.fetch_message(ids[-1])
                        await message.delete()
                    except discord.NotFound:
                        pass
                    ids.pop()
            except discord.HTTPException:
                log.warning(
                    "Could not refresh DJ board for guild %s, day %s", guild.id, key, exc_info=True
                )
            finally:
                if ids:
                    async with self.config.guild(guild).schedules() as current:
                        if key in current:
                            current[key]["message_ids"] = ids
                            current[key]["message_id"] = ids[0]

    async def _post(
        self, guild: discord.Guild, key: str, channel: discord.TextChannel
    ) -> Optional[discord.Message]:
        async with self._board_locks[guild.id]:
            messages = []
            try:
                for embed in await self._embeds(guild, key):
                    messages.append(
                        await channel.send(
                            embed=embed, allowed_mentions=discord.AllowedMentions.none()
                        )
                    )
            except discord.HTTPException:
                log.warning(
                    "Could not post DJ board for guild %s, day %s", guild.id, key, exc_info=True
                )
                for message in messages:
                    try:
                        await message.delete()
                    except discord.HTTPException:
                        pass
                return None
            if not messages:
                return None
            async with self.config.guild(guild).schedules() as schedules:
                if key not in schedules:
                    return None
                schedules[key]["channel_id"] = channel.id
                schedules[key]["message_id"] = messages[0].id
                schedules[key]["message_ids"] = [message.id for message in messages]
            return messages[0]

    async def _announce(self, guild: discord.Guild, text: str) -> None:
        conf = self.config.guild(guild)
        if not await conf.announce():
            return
        channel_id = await conf.announce_channel() or await conf.board_channel()
        channel = guild.get_channel(channel_id or 0)
        if isinstance(channel, discord.TextChannel):
            try:
                await channel.send(
                    text, allowed_mentions=discord.AllowedMentions(everyone=False, roles=False)
                )
            except discord.HTTPException:
                log.warning("Could not send DJ announcement in guild %s", guild.id, exc_info=True)

    # ------------------------------------------------------------------ #
    # DJ-facing commands
    # ------------------------------------------------------------------ #

    @commands.guild_only()
    @commands.group(name="dj")
    async def dj(self, ctx: commands.Context) -> None:
        """DJ slot scheduling."""

    @dj.command(name="claim")
    async def dj_claim(self, ctx: commands.Context, slot_id: int) -> None:
        """Claim a free slot by its number, e.g. `[p]dj claim 3`."""
        role_id = await self.config.guild(ctx.guild).dj_role()
        if role_id and role_id not in [role.id for role in ctx.author.roles]:
            role = ctx.guild.get_role(role_id)
            await ctx.send(
                f"Only members with the **{role.name if role else 'DJ'}** role can claim slots."
            )
            return

        async with self.config.guild(ctx.guild).schedules() as schedules:
            found = self._locate(schedules, slot_id)
            if not found:
                await ctx.send(self._missing(schedules, slot_id))
                return
            key, schedule = found
            slot = schedule["slots"][str(slot_id)]

            if slot["user"] == ctx.author.id:
                await ctx.send(f"You already hold slot **#{slot_id}**.")
                return
            if slot["user"]:
                await ctx.send(f"Slot **#{slot_id}** is already taken.")
                return
            if slot["ts"] <= int(datetime.now(timezone.utc).timestamp()):
                await ctx.send(f"Slot **#{slot_id}** has already started.")
                return

            mine = self._held_by(schedule, ctx.author.id)
            if mine:
                await ctx.send(
                    f"You already hold slot **#{mine[0]}** on **{key}** — one slot per DJ per "
                    f"day. Drop it first with `{ctx.clean_prefix}dj drop {mine[0]}`."
                )
                return

            slot["user"] = ctx.author.id
            ts = slot["ts"]

        day = Date.fromisoformat(key)
        await ctx.send(f"✅ Slot **#{slot_id}** is yours — {zone_line(ts, day)}")
        await self._refresh(ctx.guild, key)
        await self._announce(
            ctx.guild,
            f"\U0001f3a7 {ctx.author.mention} booked slot **#{slot_id}** on **{day:%A %d %B}**\n"
            f"{zone_line(ts, day)}  ·  <t:{ts}:t> your time",
        )

    @dj.command(name="drop")
    async def dj_drop(self, ctx: commands.Context, slot_id: int = None) -> None:
        """Give up your slot. The number is optional if you only hold one."""
        async with self.config.guild(ctx.guild).schedules() as schedules:
            if slot_id is None:
                owned: List[Tuple[str, int]] = [
                    (key, held)
                    for key, schedule in schedules.items()
                    if schedule.get("open", True)
                    for held in self._held_by(schedule, ctx.author.id)
                ]
                if not owned:
                    await ctx.send("You do not hold any slots.")
                    return
                if len(owned) > 1:
                    listed = ", ".join(f"**#{held}** ({key})" for key, held in owned)
                    await ctx.send(
                        f"You hold {listed}. Say which one: `{ctx.clean_prefix}dj drop <number>`."
                    )
                    return
                key, slot_id = owned[0]
                schedule = schedules[key]
            else:
                found = self._locate(schedules, slot_id)
                if not found:
                    await ctx.send(self._missing(schedules, slot_id))
                    return
                key, schedule = found

            slot = schedule["slots"][str(slot_id)]
            if slot["user"] != ctx.author.id:
                await ctx.send(f"Slot **#{slot_id}** is not yours.")
                return
            slot["user"] = None
            ts = slot["ts"]

        day = Date.fromisoformat(key)
        await ctx.send(f"↩️ You dropped slot **#{slot_id}**.")
        await self._refresh(ctx.guild, key)
        await self._announce(
            ctx.guild,
            f"↩️ {ctx.author.mention} released slot **#{slot_id}** on "
            f"**{day:%A %d %B}** — {zone_line(ts, day)} is free again.",
        )

    @dj.command(name="mine")
    async def dj_mine(self, ctx: commands.Context) -> None:
        """Show the slots you currently hold."""
        schedules = await self.config.guild(ctx.guild).schedules()
        lines = []
        for key in sorted(schedules):
            schedule = schedules[key]
            day = Date.fromisoformat(key)
            for held in self._held_by(schedule, ctx.author.id):
                ts = schedule["slots"][str(held)]["ts"]
                lines.append(f"`#{held}` **{day:%a %d %b}** — {zone_line(ts, day)}")
        for page in self._paginate(lines or ["You do not hold any slots."]):
            await ctx.send(page)

    @dj.command(name="free")
    async def dj_free(self, ctx: commands.Context) -> None:
        """List every slot that is still open."""
        schedules = await self.config.guild(ctx.guild).schedules()
        lines = []
        now = int(datetime.now(timezone.utc).timestamp())
        for key in sorted(schedules):
            schedule = schedules[key]
            if not schedule.get("open", True):
                continue
            day = Date.fromisoformat(key)
            free = sorted(
                (
                    int(i)
                    for i, slot in schedule["slots"].items()
                    if not slot["user"] and slot["ts"] > now
                ),
                key=lambda i, s=schedule: (s["slots"][str(i)]["ts"], i),
            )
            if not free:
                continue
            lines.append(f"__**{day:%A %d %B}**__")
            for slot_id in free:
                ts = schedule["slots"][str(slot_id)]["ts"]
                lines.append(f"`#{slot_id}` {zone_line(ts, day)}")
        if not lines:
            await ctx.send("No free slots right now.")
            return
        for page in self._paginate(lines):
            await ctx.send(page)

    @dj.command(name="board")
    async def dj_board(self, ctx: commands.Context, day: str = None) -> None:
        """Show the schedule board here. Defaults to the next scheduled day."""
        key = await self._resolve_key(ctx, day)
        if key is None:
            return
        for embed in await self._embeds(ctx.guild, key):
            await ctx.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    # ------------------------------------------------------------------ #
    # admin commands
    # ------------------------------------------------------------------ #

    @dj.command(name="create")
    @commands.admin_or_permissions(manage_guild=True)
    async def dj_create(
        self, ctx: commands.Context, day: str, start: str, end: str, slots_per_hour: int = 1
    ) -> None:
        """Open a day for signups.

        `[p]dj create friday 18:00 02:00 2` gives two slots for every hour from
        18:00 up to (but not including) 02:00, in the server's anchor timezone.
        """
        if not 1 <= slots_per_hour <= MAX_PER_HOUR:
            await ctx.send(f"Slots per hour has to be between 1 and {MAX_PER_HOUR}.")
            return

        conf = self.config.guild(ctx.guild)
        anchor = await conf.anchor_tz()
        try:
            date = parse_day(day, anchor)
            start_hour = parse_hour(start)
            end_hour = parse_hour(end)
        except ValueError as exc:
            await ctx.send(str(exc))
            return

        key = date.isoformat()
        async with conf.schedules() as schedules:
            if key in schedules:
                await ctx.send(
                    f"There is already a schedule for **{key}**. "
                    f"Delete it first with `{ctx.clean_prefix}dj delete {key}`."
                )
                return
            first_id = self._next_id(schedules)
            try:
                slots = build_slots(date, anchor, start_hour, end_hour, slots_per_hour, first_id)
            except ValueError as exc:
                await ctx.send(str(exc))
                return
            schedules[key] = {
                "tz": anchor,
                "open": True,
                "channel_id": None,
                "message_id": None,
                "message_ids": [],
                "slots": slots,
            }
            total = len(schedules[key]["slots"])

        hours = total // slots_per_hour
        await ctx.send(
            f"Created **{date:%A %d %B %Y}** — {hours} hour(s), {slots_per_hour} slot(s) "
            f"each, {total} slots numbered **#{first_id}–#{first_id + total - 1}**."
        )

        channel = ctx.guild.get_channel(await conf.board_channel() or 0)
        if not isinstance(channel, discord.TextChannel):
            channel = ctx.channel
        if await self._post(ctx.guild, key, channel) is None:
            await ctx.send(
                "The schedule was saved, but I could not post its board. Check my channel permissions and use `dj post` to retry."
            )

    @dj.command(name="capacity")
    @commands.admin_or_permissions(manage_guild=True)
    async def dj_capacity(self, ctx: commands.Context, day: str, hour: str, count: int) -> None:
        """Change how many slots one hour has, e.g. `[p]dj capacity friday 22:00 3`."""
        if not 0 <= count <= MAX_PER_HOUR:
            await ctx.send(f"Slot count has to be between 0 and {MAX_PER_HOUR}.")
            return

        key = await self._resolve_key(ctx, day)
        if key is None:
            return
        try:
            target_hour = parse_hour(hour)
        except ValueError as exc:
            await ctx.send(str(exc))
            return

        async with self.config.guild(ctx.guild).schedules() as schedules:
            schedule = schedules[key]
            tz = ZoneInfo(schedule["tz"])

            matches = sorted(
                ts
                for ts in {slot["ts"] for slot in schedule["slots"].values()}
                if datetime.fromtimestamp(ts, tz).hour == target_hour
            )
            if not matches:
                await ctx.send(f"**{key}** has no slots at that hour.")
                return
            if len(matches) > 1:
                await ctx.send(
                    "That hour occurs twice because the clocks move back. Use the numbered slots to assign or clear DJs; capacity changes for repeated hours are not supported."
                )
                return
            ts = matches[0]

            existing = sorted(
                (int(i) for i, slot in schedule["slots"].items() if slot["ts"] == ts),
                reverse=True,
            )
            current = len(existing)

            if count > current:
                next_id = self._next_id(schedules)
                for offset in range(count - current):
                    schedule["slots"][str(next_id + offset)] = {"ts": ts, "user": None}
            elif count < current:
                removable = [i for i in existing if not schedule["slots"][str(i)]["user"]]
                if len(removable) < current - count:
                    await ctx.send(
                        f"Cannot shrink that hour to {count}: "
                        f"{current - len(removable)} slot(s) are already claimed."
                    )
                    return
                for slot_id in removable[: current - count]:
                    del schedule["slots"][str(slot_id)]

        day_date = Date.fromisoformat(key)
        await ctx.send(f"{zone_line(ts, day_date)} now has **{count}** slot(s).")
        await self._refresh(ctx.guild, key)

    @dj.command(name="assign")
    @commands.admin_or_permissions(manage_guild=True)
    async def dj_assign(self, ctx: commands.Context, slot_id: int, member: discord.Member) -> None:
        """Put a member into a slot, ignoring the one-per-day limit."""
        async with self.config.guild(ctx.guild).schedules() as schedules:
            found = self._locate(schedules, slot_id, open_only=False)
            if not found:
                await ctx.send(f"There is no slot **#{slot_id}**.")
                return
            key, schedule = found
            slot = schedule["slots"][str(slot_id)]
            previous = slot["user"]
            replaced = f" (replacing <@{previous}>)" if previous and previous != member.id else ""
            slot["user"] = member.id
            ts = slot["ts"]

        day = Date.fromisoformat(key)
        await ctx.send(f"Assigned slot **#{slot_id}** to {member.display_name}{replaced}.")
        await self._refresh(ctx.guild, key)
        await self._announce(
            ctx.guild,
            f"\U0001f3a7 {member.mention} was given slot **#{slot_id}** on **{day:%A %d %B}**\n"
            f"{zone_line(ts, day)}  ·  <t:{ts}:t> your time",
        )

    @dj.command(name="clear")
    @commands.admin_or_permissions(manage_guild=True)
    async def dj_clear(self, ctx: commands.Context, slot_id: int) -> None:
        """Empty a slot, whoever holds it."""
        async with self.config.guild(ctx.guild).schedules() as schedules:
            found = self._locate(schedules, slot_id, open_only=False)
            if not found:
                await ctx.send(f"There is no slot **#{slot_id}**.")
                return
            key, schedule = found
            schedule["slots"][str(slot_id)]["user"] = None

        await ctx.send(f"Slot **#{slot_id}** is free again.")
        await self._refresh(ctx.guild, key)

    @dj.command(name="post")
    @commands.admin_or_permissions(manage_guild=True)
    async def dj_post(
        self, ctx: commands.Context, day: str, channel: discord.TextChannel = None
    ) -> None:
        """Post (or re-post) the live board for a day."""
        key = await self._resolve_key(ctx, day)
        if key is None:
            return
        target = channel or ctx.channel
        message = await self._post(ctx.guild, key, target)
        if message is None:
            await ctx.send(f"I cannot post in {target.mention}.")
        elif target != ctx.channel:
            await ctx.send(f"Board posted in {target.mention}.")

    @dj.command(name="refresh")
    @commands.admin_or_permissions(manage_guild=True)
    async def dj_refresh(self, ctx: commands.Context, day: str = None) -> None:
        """Force the posted board to redraw."""
        key = await self._resolve_key(ctx, day)
        if key is None:
            return
        await self._refresh(ctx.guild, key)
        await ctx.tick()

    @dj.command(name="close")
    @commands.admin_or_permissions(manage_guild=True)
    async def dj_close(self, ctx: commands.Context, day: str = None) -> None:
        """Stop new signups for a day, keeping everything that was claimed."""
        key = await self._resolve_key(ctx, day)
        if key is None:
            return
        async with self.config.guild(ctx.guild).schedules() as schedules:
            schedules[key]["open"] = False
        await ctx.send(f"Signups for **{key}** are closed.")
        await self._refresh(ctx.guild, key)

    @dj.command(name="reopen")
    @commands.admin_or_permissions(manage_guild=True)
    async def dj_reopen(self, ctx: commands.Context, day: str) -> None:
        """Re-open signups for a day."""
        key = await self._resolve_key(ctx, day)
        if key is None:
            return
        async with self.config.guild(ctx.guild).schedules() as schedules:
            others = {
                int(slot_id)
                for other_key, schedule in schedules.items()
                if other_key != key and schedule.get("open", True)
                for slot_id in schedule["slots"]
            }
            clashing = others & {int(i) for i in schedules[key]["slots"]}
            if clashing:
                await ctx.send(
                    "Cannot reopen: slot numbers "
                    + ", ".join(f"#{i}" for i in sorted(clashing))
                    + " are already in use by another open day."
                )
                return
            schedules[key]["open"] = True
        await ctx.send(f"Signups for **{key}** are open again.")
        await self._refresh(ctx.guild, key)

    @dj.command(name="delete")
    @commands.admin_or_permissions(manage_guild=True)
    async def dj_delete(self, ctx: commands.Context, day: str) -> None:
        """Delete a day and everyone's claims on it."""
        key = await self._resolve_key(ctx, day)
        if key is None:
            return
        async with self.config.guild(ctx.guild).schedules() as schedules:
            taken = sum(1 for slot in schedules[key]["slots"].values() if slot["user"])
            del schedules[key]
        await ctx.send(f"Deleted **{key}** ({taken} claimed slot(s) removed).")

    @dj.command(name="days")
    @commands.admin_or_permissions(manage_guild=True)
    async def dj_days(self, ctx: commands.Context) -> None:
        """List every day currently on file."""
        schedules = await self.config.guild(ctx.guild).schedules()
        if not schedules:
            await ctx.send("No days scheduled.")
            return
        lines = []
        for key in sorted(schedules):
            schedule = schedules[key]
            slots = schedule["slots"]
            taken = sum(1 for slot in slots.values() if slot["user"])
            ids = sorted(int(i) for i in slots)
            state = "open" if schedule.get("open", True) else "closed"
            lines.append(
                f"**{key}** — {taken}/{len(slots)} taken, "
                + (f"slots #{ids[0]}–#{ids[-1]}" if ids else "no slots")
                + f" ({state})"
            )
        for page in self._paginate(lines):
            await ctx.send(page)

    # ------------------------------------------------------------------ #
    # settings
    # ------------------------------------------------------------------ #

    @dj.group(name="set")
    @commands.admin_or_permissions(manage_guild=True)
    async def dj_set(self, ctx: commands.Context) -> None:
        """DJScheduler settings."""

    @dj_set.command(name="timezone")
    async def set_timezone(self, ctx: commands.Context, tz: str) -> None:
        """Set the timezone admins type hours in. Defaults to EST."""
        try:
            resolved = resolve_tz(tz)
        except ValueError as exc:
            await ctx.send(str(exc))
            return
        await self.config.guild(ctx.guild).anchor_tz.set(resolved)
        now = datetime.now(ZoneInfo(resolved))
        await ctx.send(
            f"Hours are now read as **{resolved}** time. "
            f"Days that already exist keep the timezone they were created with.\n"
            f"Right now that is {zone_line(int(now.timestamp()), now.date())}."
        )

    @dj_set.command(name="board")
    async def set_board(self, ctx: commands.Context, channel: discord.TextChannel = None) -> None:
        """Set the channel new boards are posted in."""
        await self.config.guild(ctx.guild).board_channel.set(channel.id if channel else None)
        await ctx.send(f"Board channel: {channel.mention if channel else 'not set'}.")

    @dj_set.command(name="announce")
    async def set_announce(
        self, ctx: commands.Context, channel: discord.TextChannel = None
    ) -> None:
        """Set where claim announcements go. No channel means the board channel."""
        conf = self.config.guild(ctx.guild)
        await conf.announce_channel.set(channel.id if channel else None)
        await conf.announce.set(True)
        await ctx.send(
            f"Claim announcements go to {channel.mention if channel else 'the board channel'}."
        )

    @dj_set.command(name="silent")
    async def set_silent(self, ctx: commands.Context, on_off: bool) -> None:
        """`true` turns claim announcements off, leaving only the live board."""
        await self.config.guild(ctx.guild).announce.set(not on_off)
        await ctx.send("Claim announcements are " + ("off." if on_off else "on."))

    @dj_set.command(name="djrole")
    async def set_djrole(self, ctx: commands.Context, role: discord.Role = None) -> None:
        """Restrict claiming to one role. No role means anyone can claim."""
        await self.config.guild(ctx.guild).dj_role.set(role.id if role else None)
        await ctx.send(f"Claiming restricted to: {role.name if role else 'everyone'}.")

    @dj_set.command(name="show")
    async def set_show(self, ctx: commands.Context) -> None:
        """Show the current settings."""
        conf = self.config.guild(ctx.guild)
        board = ctx.guild.get_channel(await conf.board_channel() or 0)
        announce = ctx.guild.get_channel(await conf.announce_channel() or 0)
        role = ctx.guild.get_role(await conf.dj_role() or 0)

        embed = discord.Embed(title="DJScheduler settings", colour=discord.Colour.blurple())
        embed.add_field(name="Anchor timezone", value=await conf.anchor_tz(), inline=False)
        embed.add_field(
            name="Board channel", value=board.mention if board else "not set", inline=False
        )
        embed.add_field(
            name="Announcements",
            value=(announce.mention if announce else "board channel")
            if await conf.announce()
            else "off",
            inline=False,
        )
        embed.add_field(name="DJ role", value=role.name if role else "everyone", inline=False)
        embed.add_field(name="Slots per DJ per day", value="1", inline=False)
        embed.add_field(name="Always shown in", value=", ".join(DISPLAY_ZONES), inline=False)
        await ctx.send(embed=embed)

    # ------------------------------------------------------------------ #
    # shared helpers
    # ------------------------------------------------------------------ #

    async def _resolve_key(self, ctx: commands.Context, day: Optional[str]) -> Optional[str]:
        """Turn a day argument into a stored schedule key, complaining if it misses."""
        schedules = await self.config.guild(ctx.guild).schedules()
        if not schedules:
            await ctx.send(f"No days scheduled yet. Start one with `{ctx.clean_prefix}dj create`.")
            return None

        anchor = await self.config.guild(ctx.guild).anchor_tz()
        if day is None:
            today = datetime.now(ZoneInfo(anchor)).date().isoformat()
            upcoming = sorted(key for key in schedules if key >= today)
            return upcoming[0] if upcoming else sorted(schedules)[-1]

        try:
            key = parse_day(day, anchor).isoformat()
        except ValueError as exc:
            await ctx.send(str(exc))
            return None
        if key not in schedules:
            await ctx.send(f"There is no schedule for **{key}**.")
            return None
        return key

    @staticmethod
    def _paginate(lines: List[str], limit: int = 1900) -> List[str]:
        pages, current = [], ""
        for line in lines:
            if len(current) + len(line) + 1 > limit:
                pages.append(current)
                current = ""
            current += f"{line}\n"
        if current:
            pages.append(current)
        return pages
