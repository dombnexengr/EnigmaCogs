"""Regression tests using real Red commands, Config storage and Discord embeds."""

import asyncio
import json
import re
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import discord
import pytest
from redbot.core import Config
from redbot.core._drivers.json import JsonDriver

import djscheduler
from djscheduler.djscheduler import DJScheduler
from djscheduler.helpers import ZoneInfo, build_slots, parse_hour, resolve_tz, zone_line


def make_schedule(day="2099-09-25", start=18, end=20, capacity=1, first_id=1):
    return {
        "tz": "America/New_York",
        "open": True,
        "channel_id": None,
        "message_id": None,
        "slots": build_slots(
            date.fromisoformat(day), "America/New_York", start, end, capacity, first_id
        ),
    }


@pytest.fixture
def scheduler(tmp_path, monkeypatch):
    identifier = str(uuid4().int)
    driver = JsonDriver("DJSchedulerTests" + identifier, identifier, data_path_override=tmp_path)
    config = Config(
        cog_name="DJSchedulerTests" + identifier,
        unique_identifier=identifier,
        driver=driver,
        force_registration=True,
    )
    monkeypatch.setattr(Config, "get_conf", lambda *args, **kwargs: config)
    bot = SimpleNamespace(
        get_valid_prefixes=AsyncMock(return_value=["<@123> ", "!"]),
        get_guild=MagicMock(return_value=None),
        add_cog=AsyncMock(),
    )
    cog = DJScheduler(bot)
    guild = SimpleNamespace(id=123, get_member=lambda _: None, get_channel=lambda _: None)
    return cog, guild


def context(guild, user_id=42):
    return SimpleNamespace(
        guild=guild,
        author=SimpleNamespace(id=user_id, roles=[], mention=f"<@{user_id}>"),
        clean_prefix="!",
        send=AsyncMock(),
        tick=AsyncMock(),
    )


@pytest.mark.parametrize(
    ("raw", "expected"), [("20:00", 20), ("20", 20), ("8pm", 20), ("12am", 0), ("12pm", 12)]
)
def test_hour_formats(raw, expected):
    assert parse_hour(raw) == expected


@pytest.mark.parametrize("raw", ["24", "8:30", "0pm", "13am", "hello"])
def test_invalid_hours(raw):
    with pytest.raises(ValueError):
        parse_hour(raw)


def test_timezone_aliases_and_validation():
    assert resolve_tz("est") == "America/New_York"
    assert resolve_tz("Europe/Athens") == "Europe/Athens"
    with pytest.raises(ValueError):
        resolve_tz("Missing/Timezone")


def test_overnight_slots_and_exclusive_end():
    slots = build_slots(date(2026, 9, 25), "America/New_York", 18, 2, 2, 14)
    assert len(slots) == 16
    assert list(slots) == [str(i) for i in range(14, 30)]
    final = datetime.fromtimestamp(slots["29"]["ts"], ZoneInfo("America/New_York"))
    assert (final.day, final.hour) == (26, 1)


@pytest.mark.parametrize(
    ("day", "hours"),
    [
        (date(2026, 3, 8), ["00 EST", "01 EST", "03 EDT"]),
        (date(2026, 11, 1), ["00 EDT", "01 EDT", "01 EST", "02 EST", "03 EST"]),
    ],
)
def test_dst_keeps_local_end_boundary(day, hours):
    slots = build_slots(day, "America/New_York", 0, 4, 1, 1)
    stamps = [slot["ts"] for slot in slots.values()]
    assert [
        datetime.fromtimestamp(ts, ZoneInfo("America/New_York")).strftime("%H %Z") for ts in stamps
    ] == hours
    assert all(b - a == 3600 for a, b in zip(stamps, stamps[1:]))


@pytest.mark.parametrize(("day", "count"), [(date(2026, 3, 8), 23), (date(2026, 11, 1), 25)])
def test_full_local_day_can_have_23_or_25_hours(day, count):
    assert len(build_slots(day, "America/New_York", 0, 0, 1, 1)) == count


@pytest.mark.parametrize(("start", "end"), [(2, 4), (0, 2)])
def test_nonexistent_boundaries_are_rejected(start, end):
    with pytest.raises(ValueError, match="does not exist"):
        build_slots(date(2026, 3, 8), "America/New_York", start, end, 1, 1)


def test_fractional_dst_transition_is_explicitly_rejected():
    with pytest.raises(ValueError, match="whole one-hour"):
        build_slots(date(2026, 10, 4), "Australia/Lord_Howe", 0, 4, 1, 1)


def test_rendered_timezones_and_date_rollover():
    ts = int(datetime(2026, 9, 26, 0, tzinfo=timezone.utc).timestamp())
    assert zone_line(ts, date(2026, 9, 25)) == "20:00 EDT · 02:00 CEST (+1d) · 17:00 PDT"


@pytest.mark.asyncio
async def test_setup_and_data_statement(scheduler):
    cog, _ = scheduler
    await djscheduler.setup(cog.bot)
    cog.bot.add_cog.assert_awaited_once()
    metadata = json.loads((Path(djscheduler.__file__).parent / "info.json").read_text())
    assert djscheduler.__red_end_user_data_statement__ == metadata["end_user_data_statement"]
    assert metadata["min_python_version"] == [3, 9, 0]


def test_admin_commands_have_permission_checks():
    for name in ["dj_create", "dj_capacity", "dj_assign", "dj_clear", "dj_delete", "dj_set"]:
        assert getattr(DJScheduler, name).requires.user_perms.manage_guild


@pytest.mark.asyncio
async def test_closed_day_capacity_never_overwrites_claims(scheduler):
    cog, guild = scheduler
    schedule = make_schedule()
    schedule["open"] = False
    schedule["slots"]["1"]["user"] = 42
    await cog.config.guild(guild).schedules.set({"2099-09-25": schedule})
    ctx = context(guild)
    await cog.dj_capacity.callback(cog, ctx, "2099-09-25", "18", 3)
    saved = (await cog.config.guild(guild).schedules())["2099-09-25"]["slots"]
    assert len(saved) == 4
    assert saved["1"]["user"] == 42
    assert set(saved) == {"1", "2", "3", "4"}


@pytest.mark.asyncio
async def test_capacity_cannot_remove_claimed_slots(scheduler):
    cog, guild = scheduler
    schedule = make_schedule()
    schedule["slots"]["1"]["user"] = 42
    await cog.config.guild(guild).schedules.set({"2099-09-25": schedule})
    ctx = context(guild)
    await cog.dj_capacity.callback(cog, ctx, "2099-09-25", "18", 0)
    assert (await cog.config.guild(guild).schedules())["2099-09-25"] == schedule
    assert "already claimed" in ctx.send.call_args.args[0]


@pytest.mark.asyncio
async def test_repeated_hour_capacity_is_not_silently_applied_to_first_occurrence(scheduler):
    cog, guild = scheduler
    schedule = make_schedule("2026-11-01", 0, 4)
    await cog.config.guild(guild).schedules.set({"2026-11-01": schedule})
    ctx = context(guild)
    await cog.dj_capacity.callback(cog, ctx, "2026-11-01", "1", 3)
    assert "occurs twice" in ctx.send.call_args.args[0]
    assert (await cog.config.guild(guild).schedules())["2026-11-01"] == schedule


@pytest.mark.asyncio
async def test_invalid_dst_creation_saves_nothing(scheduler):
    cog, guild = scheduler
    ctx = context(guild)
    await cog.dj_create.callback(cog, ctx, "2026-03-08", "2", "4")
    assert await cog.config.guild(guild).schedules() == {}
    assert "does not exist" in ctx.send.call_args.args[0]


@pytest.mark.asyncio
async def test_concurrent_claims_cannot_double_book(scheduler):
    cog, guild = scheduler
    await cog.config.guild(guild).schedules.set({"2099-09-25": make_schedule()})
    first, second = context(guild, 42), context(guild, 43)
    await asyncio.gather(
        cog.dj_claim.callback(cog, first, 1),
        cog.dj_claim.callback(cog, second, 1),
    )
    slots = (await cog.config.guild(guild).schedules())["2099-09-25"]["slots"]
    assert slots["1"]["user"] in (42, 43)
    responses = [call.args[0] for ctx in (first, second) for call in ctx.send.call_args_list]
    assert sum("is yours" in response for response in responses) == 1
    assert sum("already taken" in response for response in responses) == 1


@pytest.mark.asyncio
async def test_concurrent_claims_keep_one_slot_per_dj(scheduler):
    cog, guild = scheduler
    await cog.config.guild(guild).schedules.set({"2099-09-25": make_schedule()})
    await asyncio.gather(*[cog.dj_claim.callback(cog, context(guild), i) for i in (1, 2)])
    slots = (await cog.config.guild(guild).schedules())["2099-09-25"]["slots"]
    assert sum(slot["user"] == 42 for slot in slots.values()) == 1


@pytest.mark.asyncio
async def test_closed_schedule_cannot_be_claimed(scheduler):
    cog, guild = scheduler
    schedule = make_schedule()
    schedule["open"] = False
    await cog.config.guild(guild).schedules.set({"2099-09-25": schedule})
    ctx = context(guild)
    await cog.dj_claim.callback(cog, ctx, 1)
    assert "closed" in ctx.send.call_args.args[0]
    assert (await cog.config.guild(guild).schedules())["2099-09-25"] == schedule


@pytest.mark.asyncio
async def test_large_board_respects_every_discord_limit(scheduler):
    cog, guild = scheduler
    schedule = make_schedule("2026-11-01", 0, 0, 10)
    for slot in schedule["slots"].values():
        slot["user"] = 1234567890123456789
    await cog.config.guild(guild).schedules.set({"2026-11-01": schedule})
    embeds = await cog._embeds(guild, "2026-11-01")
    assert len(embeds) > 1
    seen = []
    for embed in embeds:
        assert len(embed) <= 6000
        assert len(embed.description) <= 4096
        assert len(embed.fields) <= 25
        assert all(len(field.value) <= 1024 for field in embed.fields)
        assert "!dj claim" in embed.description
        seen.extend(re.findall(r"`#(\d+)`", embed.description))
    assert len(seen) == 250
    assert set(seen) == set(schedule["slots"])


def fake_channel():
    channel = MagicMock(spec=discord.TextChannel)
    channel.id = 456
    messages = {}

    async def send(**kwargs):
        message = SimpleNamespace(id=1000 + len(messages), edit=AsyncMock(), delete=AsyncMock())
        messages[message.id] = message
        return message

    async def fetch(message_id):
        if message_id not in messages:
            raise discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "Missing")
        return messages[message_id]

    channel.send = AsyncMock(side_effect=send)
    channel.fetch_message = AsyncMock(side_effect=fetch)
    return channel, messages


@pytest.mark.asyncio
async def test_board_refresh_adds_pages_and_removes_surplus(scheduler):
    cog, guild = scheduler
    channel, messages = fake_channel()
    guild.get_channel = lambda _: channel
    await cog.config.guild(guild).schedules.set(
        {"2099-09-25": make_schedule(start=0, end=0, capacity=10)}
    )
    await cog._post(guild, "2099-09-25", channel)
    original_ids = list(messages)
    assert len(original_ids) > 1
    async with cog.config.guild(guild).schedules() as schedules:
        schedules["2099-09-25"]["slots"] = make_schedule()["slots"]
    await cog._refresh(guild, "2099-09-25")
    saved = (await cog.config.guild(guild).schedules())["2099-09-25"]
    assert saved["message_ids"] == original_ids[:1]
    messages[original_ids[0]].edit.assert_awaited_once()
    for message_id in original_ids[1:]:
        messages[message_id].delete.assert_awaited_once()


@pytest.mark.asyncio
async def test_refresh_keeps_partial_progress_after_http_failure(scheduler):
    cog, guild = scheduler
    channel, messages = fake_channel()
    guild.get_channel = lambda _: channel
    schedule = make_schedule(start=0, end=0, capacity=10)
    schedule.update(channel_id=channel.id, message_id=999)
    await cog.config.guild(guild).schedules.set({"2099-09-25": schedule})
    normal_send = channel.send.side_effect

    async def fail_after_first_page(**kwargs):
        if messages:
            raise discord.Forbidden(
                SimpleNamespace(status=403, reason="Forbidden"), "Test failure"
            )
        return await normal_send(**kwargs)

    channel.send.side_effect = fail_after_first_page
    await cog._refresh(guild, "2099-09-25")
    saved = (await cog.config.guild(guild).schedules())["2099-09-25"]
    assert saved["message_ids"] == [1000]
    channel.send.side_effect = normal_send
    await cog._refresh(guild, "2099-09-25")
    saved = (await cog.config.guild(guild).schedules())["2099-09-25"]
    assert len(saved["message_ids"]) > 1
    messages[1000].edit.assert_awaited_once()


@pytest.mark.asyncio
async def test_empty_schedules_can_be_listed(scheduler):
    cog, guild = scheduler
    schedule = make_schedule()
    schedule["slots"] = {}
    await cog.config.guild(guild).schedules.set({"2099-09-25": schedule})
    ctx = context(guild)
    await cog.dj_days.callback(cog, ctx)
    assert "no slots" in ctx.send.call_args.args[0]


@pytest.mark.asyncio
async def test_large_personal_schedule_list_is_paginated(scheduler):
    cog, guild = scheduler
    schedule = make_schedule(start=0, end=0, capacity=10)
    for slot in schedule["slots"].values():
        slot["user"] = 42
    await cog.config.guild(guild).schedules.set({"2099-09-25": schedule})
    ctx = context(guild)
    await cog.dj_mine.callback(cog, ctx)
    assert ctx.send.await_count > 1
    assert all(len(call.args[0]) <= 2000 for call in ctx.send.call_args_list)


@pytest.mark.asyncio
async def test_expired_slots_are_not_advertised_as_free(scheduler):
    cog, guild = scheduler
    await cog.config.guild(guild).schedules.set({"2000-01-01": make_schedule("2000-01-01")})
    ctx = context(guild)
    await cog.dj_free.callback(cog, ctx)
    ctx.send.assert_awaited_once_with("No free slots right now.")


@pytest.mark.asyncio
async def test_user_deletion_clears_only_requested_user(scheduler):
    cog, guild = scheduler
    schedule = make_schedule()
    schedule["slots"]["1"]["user"] = 42
    schedule["slots"]["2"]["user"] = 43
    await cog.config.guild(guild).schedules.set({"2099-09-25": schedule})
    cog.bot.get_guild.return_value = guild
    cog._refresh = AsyncMock()
    await cog.red_delete_data_for_user(requester="discord_deleted_user", user_id=42)
    slots = (await cog.config.guild(guild).schedules())["2099-09-25"]["slots"]
    assert slots["1"]["user"] is None
    assert slots["2"]["user"] == 43
    cog._refresh.assert_awaited_once_with(guild, "2099-09-25")
