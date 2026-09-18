# DJScheduler

Hourly DJ signups for [Red-DiscordBot](https://github.com/Cog-Creators/Red-DiscordBot),
part of [EnigmaCogs](https://github.com/dombnexengr/EnigmaCogs).

Create a schedule, choose how many DJs can play each hour, and let members claim
numbered slots. Live boards show Eastern, Central European and Pacific time
together. Optional announcements also include Discord's local-time display.

## Requirements

- Red 3.5+ on a Python version supported by your Red release; this cog needs Python 3.9+.
- Red's Downloader installs the `tzdata` dependency automatically.
- Board channel permissions: **View Channel**, **Send Messages**, **Embed Links**
  and **Read Message History**. Announcements need View Channel and Send Messages.

## Install

Run these as the bot owner. Replace `[p]` with your bot's prefix, such as `!`.

```text
[p]load downloader
[p]repo add EnigmaCogs https://github.com/dombnexengr/EnigmaCogs
[p]cog install EnigmaCogs djscheduler
[p]load djscheduler
```

If EnigmaCogs is already added, use `[p]repo update EnigmaCogs` instead of adding
it again. Use the repository name you originally chose if it differs.

To update an installed copy:

```text
[p]cog update djscheduler
[p]reload djscheduler
```

## Quick setup

Administrators choose the timezone for entering hours, then the channels:

```text
[p]dj set timezone Europe/Athens
[p]dj set board #dj-schedule
[p]dj set announce #dj-bookings
[p]dj set djrole @DJ
[p]dj create friday 18:00 02:00 2
```

This example creates two slots per hour from Friday at 18:00 through Saturday
at 01:00 in Athens time. The end, 02:00, is excluded. On an ordinary night this
means eight hours and sixteen slots. Members claim with `[p]dj claim 3`.

The announcement channel and DJ role are optional. Without a role restriction,
any member can claim. Without an announcement channel, announcements use the
configured board channel. `[p]dj set silent true` disables announcements.

The default input timezone is `America/New_York`. Aliases `EST`, `CET` and `PST`
select regional timezones that follow daylight saving, not fixed UTC offsets.
Full names such as `Europe/Athens` also work. Every board still shows the three
standard display zones.

## Commands

### Members

| Command | Description |
| --- | --- |
| `[p]dj claim <number>` | Claim a future, free slot on an open day. |
| `[p]dj drop [number]` | Release your slot on an open day; omit the number if you hold only one open slot. |
| `[p]dj mine` | List your bookings, including closed days. |
| `[p]dj free` | List future, free slots on open days. |
| `[p]dj board [day]` | Show a snapshot of a schedule here. |

### Administrators

Requires Red administrator privileges or Discord's **Manage Server** permission.

| Command | Description |
| --- | --- |
| `[p]dj create <day> <start> <end> [slots]` | Create a day with 1–10 slots per hour; defaults to 1. |
| `[p]dj capacity <day> <hour> <count>` | Set an existing hour's capacity to 0–10; cannot remove booked slots. |
| `[p]dj assign <number> <member>` | Assign a member, overriding role and one-slot limits; can replace a booking. |
| `[p]dj clear <number>` | Clear any booking. |
| `[p]dj post <day> [channel]` | Post a new live board; defaults to the current channel. |
| `[p]dj refresh [day]` | Refresh the tracked live board. |
| `[p]dj close [day]` | Lock member claims and drops while keeping bookings. |
| `[p]dj reopen <day>` | Reopen a closed schedule. |
| `[p]dj delete <day>` | Delete the saved schedule and its claims. |
| `[p]dj days` | List all saved schedules. |
| `[p]dj set timezone <zone>` | Set the timezone for new schedules. |
| `[p]dj set board [channel]` | Set or clear the default board channel. |
| `[p]dj set announce [channel]` | Set the announcement channel, or use the board channel; enables announcements. |
| `[p]dj set silent <true/false>` | Disable or enable announcements. |
| `[p]dj set djrole [role]` | Restrict claiming to a role, or clear the restriction. |
| `[p]dj set show` | Show settings. |

## Scheduling rules

- Days accept `YYYY-MM-DD`, `today`, `tonight`, `tomorrow` or a weekday. A weekday
  means its next occurrence; `friday` on Friday selects the following week.
  Use an explicit date to manage a particular existing schedule.
- Hours accept `20:00`, `20` or `8pm`. Minutes must be zero.
- An end at or before the start falls on the next day. Matching start and end
  hours mean a full local day.
- Clock changes can produce 23 or 25 hours in a full day. Slots stay one real
  hour long and the local end is excluded. Missing hours are skipped; repeated
  hours have separate slot numbers and timezone labels.
- A nonexistent start or end hour is rejected. An ambiguous boundary uses its
  first occurrence. Ranges crossing fractional-hour clock changes are rejected
  because they cannot be divided into whole one-hour slots.
- Capacity changes for repeated clock hours are rejected to avoid changing the
  wrong occurrence; assign or clear their numbered slots instead. Setting
  capacity to zero removes an hour; `capacity` cannot recreate it.
- Members can claim one slot per schedule. Administrators can override this.
- IDs remain reserved in open and closed schedules, preventing collisions and
  overwritten bookings. Numbers may be reused after deleting their schedule.
  Legacy schedules with overlapping IDs cannot reopen while an open day uses
  the same numbers.
- Every schedule keeps its original timezone after settings change.
- Large live boards span several messages. Only the most recently posted board
  for each day is tracked. Older reposts and `dj board` copies are snapshots.
  Deleting a schedule does not purge historical Discord messages.

## Stored data

The cog stores guild settings, dates, slot times, booked DJs' Discord IDs and
channel/message IDs for live boards. Red data deletion requests clear a user's
saved claims and refresh accessible tracked boards. Historical announcements
and copied boards remain in Discord unless manually deleted.

## Development

From the repository root, use a virtual environment with Python 3.9–3.11,
which is supported by the pinned Red test dependency:

```sh
python -m pip install -r tests/djscheduler/requirements.txt
python -m pytest tests/djscheduler -q -o asyncio_default_fixture_loop_scope=function
python -m ruff check djscheduler tests/djscheduler
python -m black --check djscheduler tests/djscheduler
```

Tests use Red's real Config storage and Discord's Embed class with mocked
network calls. No bot token is needed. After installation, smoke-test commands
in a test Discord channel before opening signups.

See the [Red publishing guide](https://docs.discord.red/en/stable/guide_publish_cogs.html)
for cog metadata and the [Discord message documentation](https://docs.discord.com/developers/resources/message)
for embed limits. The repository's existing [license](../LICENSE) applies.
