import asyncio
import contextlib
import json
import logging
import re
import time
from dateutil.parser import parse as parse_time
from random import choice
from string import ascii_letters
from datetime import datetime, timedelta, timezone
import xml.etree.ElementTree as ET
from typing import ClassVar, Optional, List, Tuple

import aiohttp
import discord

try:
    from curl_cffi.requests import AsyncSession as _CurlSession
    _CURL_AVAILABLE = True
except ImportError:
    _CURL_AVAILABLE = False

from .errors import (
    APIError,
    OfflineStream,
    InvalidTwitchCredentials,
    InvalidYoutubeCredentials,
    StreamNotFound,
    InvalidTrovoCredentials,
    YoutubeQuotaExceeded,
)
from redbot.core.i18n import Translator
from redbot.core.utils.chat_formatting import humanize_number, humanize_timedelta

TWITCH_BASE_URL = "https://api.twitch.tv"
TWITCH_ID_ENDPOINT = TWITCH_BASE_URL + "/helix/users"
TWITCH_STREAMS_ENDPOINT = TWITCH_BASE_URL + "/helix/streams/"
YOUTUBE_BASE_URL = "https://www.googleapis.com/youtube/v3"
YOUTUBE_CHANNELS_ENDPOINT = YOUTUBE_BASE_URL + "/channels"
YOUTUBE_SEARCH_ENDPOINT = YOUTUBE_BASE_URL + "/search"
YOUTUBE_VIDEOS_ENDPOINT = YOUTUBE_BASE_URL + "/videos"
YOUTUBE_CHANNEL_RSS = "https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"

TROVO_BASE_URL = "https://open-api.trovo.live/openplatform"
TROVO_GETUSERS_ENDPOINT = TROVO_BASE_URL + "/getusers"
TROVO_CHANNELINFO_ENDPOINT = TROVO_BASE_URL + "/channels/id"

KICK_BASE_URL = "https://kick.com/api/v2"
KICK_CHANNELS_ENDPOINT = KICK_BASE_URL + "/channels/"
KICK_V1_CHANNELS_ENDPOINT = "https://kick.com/api/v1/channels/"

TIKTOK_LIVE_URL = "https://www.tiktok.com/@{username}/live"

_ = Translator("Streams", __file__)

log = logging.getLogger("red.core.cogs.Streams")


def rnd(url):
    """Appends a random parameter to the url to avoid Discord's caching"""
    return url + "?rnd=" + "".join([choice(ascii_letters) for _loop_counter in range(6)])


def get_video_ids_from_feed(feed):
    root = ET.fromstring(feed)
    rss_video_ids = []
    for child in root.iter("{http://www.w3.org/2005/Atom}entry"):
        for i in child.iter("{http://www.youtube.com/xml/schemas/2015}videoId"):
            yield i.text


class Stream:

    token_name: ClassVar[Optional[str]] = None
    platform_name: ClassVar[Optional[str]] = None

    def __init__(self, **kwargs):
        self._bot = kwargs.pop("_bot")
        self.name = kwargs.pop("name", None)
        self.channels = kwargs.pop("channels", [])
        # self.already_online = kwargs.pop("already_online", False)
        self.messages = kwargs.pop("messages", [])
        self.type = self.__class__.__name__
        # Keep track of how many failed consecutive attempts we had at checking
        # if the stream's channel actually exists.
        self.retry_count = 0

    @property
    def display_name(self) -> Optional[str]:
        return self.name

    async def is_online(self):
        raise NotImplementedError()

    def make_embed(self):
        raise NotImplementedError()

    def iter_messages(self):
        for msg_data in self.messages:
            data = msg_data.copy()
            # "guild" key might not exist for old config data (available since GH-4742)
            if guild_id := msg_data.get("guild"):
                guild = self._bot.get_guild(guild_id)
                channel = guild and guild.get_channel(msg_data["channel"])
            else:
                channel = self._bot.get_channel(msg_data["channel"])

            data["partial_message"] = (
                channel.get_partial_message(data["message"]) if channel is not None else None
            )
            yield data

    def export(self):
        data = {}
        for k, v in self.__dict__.items():
            if not k.startswith("_"):
                data[k] = v
        return data

    def __repr__(self):
        return "<{0.__class__.__name__}: {0.name}>".format(self)


class YoutubeStream(Stream):

    token_name = "youtube"
    platform_name = "YouTube"

    def __init__(self, **kwargs):
        self.id = kwargs.pop("id", None)
        self._token = kwargs.pop("token", None)
        self._config = kwargs.pop("config")
        # Persist the not_livestreams cache across reloads so we don't re-check
        # every RSS video on restart (saves ~1 API unit per channel per reload).
        # Cap at 50 entries — the RSS feed only shows 15 videos so anything older
        # than that will never appear again.
        raw_not_live = kwargs.pop("not_livestreams", [])
        self.not_livestreams: List[str] = raw_not_live[-50:] if raw_not_live else []
        # Reset livestreams on reload so we re-announce after a restart.
        kwargs.pop("livestreams", None)
        self.livestreams: List[str] = []

        super().__init__(**kwargs)

    async def is_online(self):
        if not self._token:
            raise InvalidYoutubeCredentials("YouTube API key is not set.")

        key = self._token.get("api_key", "") if self._token else ""
        log.debug(
            "YouTube %s: using API key prefix=%s... suffix=...%s",
            self.name,
            key[:4] if key else "NONE",
            key[-4:] if key else "NONE",
        )

        if not self.id:
            self.id = await self.fetch_id()
        elif not self.name:
            self.name = await self.fetch_name()

        async with aiohttp.ClientSession() as session:
            async with session.get(YOUTUBE_CHANNEL_RSS.format(channel_id=self.id)) as r:
                if r.status == 404:
                    raise StreamNotFound()
                rssdata = await r.text()

        # Reset the retry count since we successfully got information about this
        # channel's streams
        self.retry_count = 0

        if self.not_livestreams:
            self.not_livestreams = list(dict.fromkeys(self.not_livestreams))

        if self.livestreams:
            self.livestreams = list(dict.fromkeys(self.livestreams))

        # Collect IDs not yet confirmed as non-livestreams
        ids_to_check = [
            vid for vid in get_video_ids_from_feed(rssdata)
            if vid not in self.not_livestreams
        ]
        log.debug("YouTube %s: checking %d video IDs: %s", self.name, len(ids_to_check), ids_to_check)

        if ids_to_check:
            # Single batched API call — YouTube allows up to 50 comma-separated IDs
            # and charges only 1 quota unit regardless of how many IDs are in the list.
            params = {
                "key": self._token["api_key"],
                "id": ",".join(ids_to_check),
                "part": "id,liveStreamingDetails",
            }
            async with aiohttp.ClientSession() as session:
                async with session.get(YOUTUBE_VIDEOS_ENDPOINT, params=params) as r:
                    data = await r.json()
            try:
                self._check_api_errors(data)
            except (InvalidYoutubeCredentials, YoutubeQuotaExceeded):
                # Propagate — handled in check_online (user-facing) and check_streams
                # (outer exception handler preserves stream.messages)
                raise
            except APIError as e:
                log.error("YouTube API error during batch check: %r", e)
                ids_to_check = []  # skip update, try again next cycle

            # Index returned items by video ID
            items_by_id = {item["id"]: item for item in data.get("items", [])}

            for video_id in ids_to_check:
                video_data = items_by_id.get(video_id, {})
                stream_data = video_data.get("liveStreamingDetails", {})
                log.debug("YouTube %s: %s → %s", self.name, video_id, stream_data)
                if (
                    stream_data
                    and stream_data != "None"
                    and stream_data.get("actualEndTime", None) is None
                ):
                    actual_start_time = stream_data.get("actualStartTime", None)
                    scheduled = stream_data.get("scheduledStartTime", None)
                    if scheduled is not None and actual_start_time is None:
                        scheduled = parse_time(scheduled)
                        if (scheduled - datetime.now(timezone.utc)).total_seconds() < -3600:
                            # Scheduled more than 1 hour ago and never started —
                            # treat as non-livestream so we stop re-checking it.
                            self.not_livestreams.append(video_id)
                            continue
                    elif actual_start_time is None:
                        # Has liveStreamingDetails but no start time at all —
                        # treat as non-livestream so we stop re-checking it.
                        self.not_livestreams.append(video_id)
                        continue
                    if video_id not in self.livestreams:
                        self.livestreams.append(video_id)
                else:
                    self.not_livestreams.append(video_id)
                    if video_id in self.livestreams:
                        self.livestreams.remove(video_id)

        log.debug("YouTube %s: livestreams=%s", self.name, self.livestreams)
        if self.livestreams:
            params = {
                "key": self._token["api_key"],
                "id": self.livestreams[-1],
                "part": "snippet,liveStreamingDetails",
            }
            async with aiohttp.ClientSession() as session:
                async with session.get(YOUTUBE_VIDEOS_ENDPOINT, params=params) as r:
                    data = await r.json()
            try:
                self._check_api_errors(data)
            except (YoutubeQuotaExceeded, InvalidYoutubeCredentials, APIError) as exc:
                log.debug("YouTube %s: final details fetch failed: %s", self.name, exc)
                raise OfflineStream()
            if not data.get("items"):
                log.debug("YouTube %s: details fetch returned no items", self.name)
                self.livestreams.pop()
                raise OfflineStream()
            return await self.make_embed(data)
        raise OfflineStream()

    async def make_embed(self, data):
        vid_data = data["items"][0]
        video_url = "https://youtube.com/watch?v={}".format(vid_data["id"])
        title = vid_data["snippet"]["title"]
        thumbnail = vid_data["snippet"]["thumbnails"]["medium"]["url"]
        channel_title = vid_data["snippet"]["channelTitle"]
        embed = discord.Embed(title=title, url=video_url)
        is_schedule = False
        if vid_data["liveStreamingDetails"].get("scheduledStartTime", None) is not None:
            if "actualStartTime" not in vid_data["liveStreamingDetails"]:
                start_time = parse_time(vid_data["liveStreamingDetails"]["scheduledStartTime"])
                start_in = start_time - datetime.now(timezone.utc)
                if start_in.total_seconds() > 0:
                    embed.description = _("This stream will start in {time}").format(
                        time=humanize_timedelta(
                            timedelta=timedelta(minutes=start_in.total_seconds() // 60)
                        )  # getting rid of seconds
                    )
                else:
                    embed.description = _(
                        "This stream was scheduled for {min} minutes ago"
                    ).format(min=round((start_in.total_seconds() * -1) // 60))
                embed.timestamp = start_time
                is_schedule = True
            else:
                # delete the message(s) about the stream schedule
                to_remove = []
                for msg_data in self.iter_messages():
                    if not msg_data.get("is_schedule", False):
                        continue
                    partial_msg = msg_data["partial_message"]
                    if partial_msg is not None:
                        autodelete = await self._config.guild(partial_msg.guild).autodelete()
                        if autodelete:
                            with contextlib.suppress(discord.NotFound):
                                await partial_msg.delete()
                    to_remove.append(msg_data["message"])
                self.messages = [
                    data for data in self.messages if data["message"] not in to_remove
                ]
        embed.set_author(name=channel_title)
        embed.set_image(url=rnd(thumbnail))
        embed.colour = 0x9255A5
        return embed, is_schedule

    async def fetch_id(self):
        return await self._fetch_channel_resource("id")

    async def fetch_name(self):
        snippet = await self._fetch_channel_resource("snippet")
        return snippet["title"]

    async def _fetch_channel_resource(self, resource: str):
        params = {"key": self._token["api_key"], "part": resource}
        if resource == "id":
            if self.name.startswith("@"):
                params["forHandle"] = self.name
            else:
                params["forUsername"] = self.name
        else:
            params["id"] = self.id

        async with aiohttp.ClientSession() as session:
            async with session.get(YOUTUBE_CHANNELS_ENDPOINT, params=params) as r:
                data = await r.json()
                status = r.status

        self._check_api_errors(data)

        # forUsername returns nothing for modern @handle channels — retry with forHandle
        if resource == "id" and "forUsername" in params and not data.get("items"):
            log.debug("YouTube %s: forUsername returned no results, retrying with forHandle", self.name)
            params.pop("forUsername")
            params["forHandle"] = f"@{self.name}"
            async with aiohttp.ClientSession() as session:
                async with session.get(YOUTUBE_CHANNELS_ENDPOINT, params=params) as r:
                    data = await r.json()
                    status = r.status
            self._check_api_errors(data)

        if "items" in data and len(data["items"]) == 0:
            raise StreamNotFound()
        elif "items" in data:
            return data["items"][0][resource]
        elif (
            "pageInfo" in data
            and "totalResults" in data["pageInfo"]
            and data["pageInfo"]["totalResults"] < 1
        ):
            raise StreamNotFound()
        raise APIError(status, data)

    def _check_api_errors(self, data: dict):
        if "error" in data:
            error_code = data["error"]["code"]
            if error_code == 400 and data["error"]["errors"][0]["reason"] == "keyInvalid":
                raise InvalidYoutubeCredentials()
            elif error_code == 403 and data["error"]["errors"][0]["reason"] in (
                "dailyLimitExceeded",
                "quotaExceeded",
                "rateLimitExceeded",
            ):
                key = self._token.get("api_key", "") if self._token else ""
                log.warning(
                    "YouTube quota exceeded for channel %s — API key suffix=...%s  "
                    "reason=%s",
                    self.name,
                    key[-6:] if key else "NONE",
                    data["error"]["errors"][0]["reason"],
                )
                raise YoutubeQuotaExceeded()
            raise APIError(error_code, data)

    def __repr__(self):
        return "<{0.__class__.__name__}: {0.name} (ID: {0.id})>".format(self)


class TwitchStream(Stream):

    token_name = "twitch"
    platform_name = "Twitch"

    def __init__(self, **kwargs):
        self.id = kwargs.pop("id", None)
        self._display_name = None
        self._client_id = kwargs.pop("token", None)
        self._bearer = kwargs.pop("bearer", None)
        self._rate_limit_resets: set = set()
        self._rate_limit_remaining: int = 0
        super().__init__(**kwargs)

    @property
    def display_name(self) -> Optional[str]:
        return self._display_name or self.name

    @display_name.setter
    def display_name(self, value: str) -> None:
        self._display_name = value

    async def wait_for_rate_limit_reset(self) -> None:
        """Check rate limits in response header and ensure we're following them.

        From python-twitch-client and adaptated to asyncio from Trusty-cogs:
        https://github.com/tsifrer/python-twitch-client/blob/master/twitch/helix/base.py
        https://github.com/TrustyJAID/Trusty-cogs/blob/master/twitch/twitch_api.py
        """
        current_time = int(time.time())
        self._rate_limit_resets = {x for x in self._rate_limit_resets if x > current_time}

        if self._rate_limit_remaining == 0:
            if self._rate_limit_resets:
                reset_time = next(iter(self._rate_limit_resets))
                # Calculate wait time and add 0.1s to the wait time to allow Twitch to reset
                # their counter
                wait_time = reset_time - current_time + 0.1
                await asyncio.sleep(wait_time)

    async def get_data(self, url: str, params: dict = {}) -> Tuple[Optional[int], dict]:
        header = {"Client-ID": str(self._client_id)}
        if self._bearer is not None:
            header["Authorization"] = f"Bearer {self._bearer}"
        await self.wait_for_rate_limit_reset()
        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(url, headers=header, params=params, timeout=60) as resp:
                    remaining = resp.headers.get("Ratelimit-Remaining")
                    if remaining:
                        self._rate_limit_remaining = int(remaining)
                    reset = resp.headers.get("Ratelimit-Reset")
                    if reset:
                        self._rate_limit_resets.add(int(reset))

                    if resp.status == 429:
                        log.info(
                            "Ratelimited. Trying again at %s.", datetime.fromtimestamp(int(reset))
                        )
                        resp.release()
                        return await self.get_data(url, params)

                    if resp.status != 200:
                        return resp.status, {}

                    return resp.status, await resp.json(encoding="utf-8")
            except (aiohttp.ClientConnectionError, asyncio.TimeoutError) as exc:
                log.warning("Connection error occurred when fetching Twitch stream", exc_info=exc)
                return None, {}

    async def is_online(self):
        user_profile_data = None
        if self.id is None:
            user_profile_data = await self._fetch_user_profile()

        stream_code, stream_data = await self.get_data(
            TWITCH_STREAMS_ENDPOINT, {"user_id": self.id}
        )
        if stream_code == 200:
            if not stream_data["data"]:
                raise OfflineStream()

            if user_profile_data is None:
                user_profile_data = await self._fetch_user_profile()

            final_data = dict.fromkeys(
                ("game_name", "login", "profile_image_url", "view_count")
            )

            if user_profile_data is not None:
                final_data["login"] = user_profile_data["login"]
                final_data["profile_image_url"] = user_profile_data["profile_image_url"]
                final_data["view_count"] = user_profile_data["view_count"]

            stream_data = stream_data["data"][0]
            final_data["user_name"] = self.display_name = stream_data["user_name"]
            final_data["game_name"] = stream_data["game_name"]
            final_data["thumbnail_url"] = stream_data["thumbnail_url"]
            final_data["title"] = stream_data["title"]
            final_data["type"] = stream_data["type"]

            # Reset the retry count since we successfully got information about this
            # channel's streams
            self.retry_count = 0

            return self.make_embed(final_data), final_data["type"] == "rerun"
        elif stream_code == 400:
            raise InvalidTwitchCredentials()
        elif stream_code == 404:
            raise StreamNotFound()
        else:
            raise APIError(stream_code, stream_data)

    async def _fetch_user_profile(self):
        code, data = await self.get_data(TWITCH_ID_ENDPOINT, {"login": self.name})
        if code == 200:
            if not data["data"]:
                raise StreamNotFound()
            if self.id is None:
                self.id = data["data"][0]["id"]
            return data["data"][0]
        elif code == 400:
            raise StreamNotFound()
        elif code == 401:
            raise InvalidTwitchCredentials()
        else:
            raise APIError(code, data)

    def make_embed(self, data):
        is_rerun = data["type"] == "rerun"
        url = f"https://www.twitch.tv/{data['login']}" if data["login"] is not None else None
        logo = data["profile_image_url"]
        if logo is None:
            logo = "https://static-cdn.jtvnw.net/jtv_user_pictures/xarth/404_user_70x70.png"
        status = data["title"]
        if not status:
            status = _("Untitled broadcast")
        if is_rerun:
            status += _(" - Rerun")
        embed = discord.Embed(title=status, url=url, color=0x6441A4)
        embed.set_author(name=data["user_name"])
        if data.get("view_count") is not None:
            embed.add_field(name=_("Total views"), value=humanize_number(data["view_count"]))
        embed.set_thumbnail(url=logo)
        if data["thumbnail_url"]:
            embed.set_image(url=rnd(data["thumbnail_url"].format(width=320, height=180)))
        if data["game_name"]:
            embed.set_footer(text=_("Playing: ") + data["game_name"])
        return embed

    def __repr__(self):
        return "<{0.__class__.__name__}: {0.name} (ID: {0.id})>".format(self)


class TrovoStream(Stream):
    token_name = "trovo"
    platform_name = "Trovo"

    def __init__(self, **kwargs):
        self.id = kwargs.pop("id", None)
        self._client_id = kwargs.pop("token", {}).get("client_id")
        super().__init__(**kwargs)

    async def is_online(self):
        if not self._client_id:
            raise InvalidTrovoCredentials()
        async with aiohttp.ClientSession(headers={"Client-ID": str(self._client_id)}) as session:
            if not self.id:
                self.id = await self.fetch_id(session)
            async with session.post(
                TROVO_CHANNELINFO_ENDPOINT, json={"channel_id": self.id}
            ) as response:
                data = await response.json()
                self._check_errors(response, data)
        if not data.get("is_live", False):
            raise OfflineStream()
        self.retry_count = 0
        return self.make_embed(data)

    async def fetch_id(self, session: aiohttp.ClientSession):
        async with session.post(TROVO_GETUSERS_ENDPOINT, json={"users": [self.name]}) as response:
            data = await response.json()
            self._check_errors(response, data)
        return data["users"][0]["channel_id"]

    def _check_errors(self, response: aiohttp.ClientResponse, data: dict):
        if response.status == 404:
            raise StreamNotFound()
        elif response.status == 400:
            if data["message"] == "header err":
                raise InvalidTrovoCredentials()
            elif data["message"] == "check invalid param":
                raise StreamNotFound()
            else:
                raise APIError(400, data)
        elif response.status != 200:
            raise APIError(response.status, data)

    def make_embed(self, data: dict):
        embed = discord.Embed(
            title=data["live_title"] or _("Untitled broadcast"),
            url=data["channel_url"],
            color=0x19D66B,
        )
        embed.set_author(name=data["username"])
        embed.add_field(name=_("Followers"), value=humanize_number(data["followers"]))
        if profile_pic := data["profile_pic"]:
            embed.set_thumbnail(url=rnd(profile_pic))
        if thumbnail := data["thumbnail"]:
            embed.set_image(url=rnd(thumbnail))
        if category := data["category_name"]:
            embed.set_footer(text=_("Playing: ") + category)
        return embed


_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/html, */*",
    "Accept-Language": "en-US,en;q=0.9",
}


class KickStream(Stream):
    token_name = None
    platform_name = "Kick"

    async def _fetch_kick(self, url: str):
        """Fetch a Kick URL and return (status_code, parsed_json)."""
        if _CURL_AVAILABLE:
            async with _CurlSession(impersonate="chrome120") as session:
                r = await session.get(url, allow_redirects=True)
                status = r.status_code
                try:
                    return status, json.loads(r.text)
                except (json.JSONDecodeError, ValueError):
                    log.debug("Kick %s: non-JSON body (HTTP %d): %.300s", self.name, status, r.text)
                    return status, {}
        else:
            async with aiohttp.ClientSession(headers=_BROWSER_HEADERS) as session:
                async with session.get(url, allow_redirects=True) as r:
                    try:
                        return r.status, await r.json(content_type=None)
                    except Exception:
                        return r.status, {}

    async def is_online(self):
        slug = self.name.lower()

        # Try v2 first, fall back to v1 — Kick has been gradually
        # deprecating v2 for some response fields.
        status, data = await self._fetch_kick(KICK_CHANNELS_ENDPOINT + slug)

        if status == 404:
            raise StreamNotFound()
        if status != 200:
            log.debug("Kick %s: v2 HTTP %d", self.name, status)
            raise APIError(status, {})

        log.debug("Kick %s: v2 keys=%s  livestream=%r",
                  self.name, list(data.keys()), data.get("livestream"))

        # Some v2 responses wrap everything under a "data" key
        if "data" in data and isinstance(data.get("data"), dict):
            data = data["data"]

        livestream = data.get("livestream") or data.get("current_livestream")

        # v2 didn't return a livestream object — try v1 which is more complete
        if not livestream:
            status1, data1 = await self._fetch_kick(KICK_V1_CHANNELS_ENDPOINT + slug)
            log.debug("Kick %s: v1 HTTP %d  keys=%s  livestream=%r",
                      self.name, status1, list(data1.keys()) if data1 else [],
                      data1.get("livestream") if data1 else None)
            if status1 == 200 and data1:
                livestream = data1.get("livestream") or data1.get("current_livestream")
                # v1 enriches the channel data; merge what we need for make_embed
                if not data.get("user"):
                    data["user"] = data1.get("user") or {}
                if not data.get("followers_count") and data1.get("followersCount"):
                    data["followers_count"] = data1["followersCount"]

        if not livestream:
            raise OfflineStream()

        # Normalise viewer count field name (v1 uses "viewers", v2 "viewer_count")
        if "viewers" in livestream and "viewer_count" not in livestream:
            livestream["viewer_count"] = livestream["viewers"]

        data["livestream"] = livestream
        self.retry_count = 0
        return self.make_embed(data)

    def make_embed(self, data: dict):
        livestream = data["livestream"]
        user = data.get("user") or {}
        slug = data.get("slug") or self.name
        channel_name = user.get("username") or slug

        title = livestream.get("session_title") or _("Untitled broadcast")
        url = f"https://kick.com/{slug}"
        embed = discord.Embed(title=title, url=url, color=0x53FC18)
        embed.set_author(name=channel_name)

        viewer_count = livestream.get("viewer_count")
        if viewer_count is not None:
            embed.add_field(name=_("Viewers"), value=humanize_number(viewer_count))

        followers = data.get("followers_count")
        if followers is not None:
            embed.add_field(name=_("Followers"), value=humanize_number(followers))

        if profile_pic := user.get("profile_pic"):
            embed.set_thumbnail(url=profile_pic)

        thumbnail_url = (livestream.get("thumbnail") or {}).get("url")
        if thumbnail_url:
            embed.set_image(url=rnd(thumbnail_url))

        categories = livestream.get("categories") or []
        if categories:
            embed.set_footer(text=_("Playing: ") + categories[0].get("name", ""))

        return embed


class TikTokStream(Stream):
    token_name = None
    platform_name = "TikTok"

    async def is_online(self):
        url = TIKTOK_LIVE_URL.format(username=self.name)
        headers = {**_BROWSER_HEADERS, "Referer": "https://www.tiktok.com/"}
        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.get(url, allow_redirects=True) as r:
                if r.status == 404:
                    raise StreamNotFound()
                if r.status != 200:
                    log.debug("TikTok %s: HTTP %d", self.name, r.status)
                    raise APIError(r.status, {})
                final_url = str(r.url)
                html = await r.text()

        log.debug("TikTok %s: final URL = %s", self.name, final_url)

        # HTTP redirect away from /live means offline
        if "/live" not in final_url:
            raise OfflineStream()

        room_info, user_info = self._extract_live_data(html)
        status = room_info.get("status")
        log.debug("TikTok %s: room status = %r (type %s)", self.name, status, type(status).__name__)

        # status 2 (int or str) = currently live
        if status not in (2, "2"):
            raise OfflineStream()

        self.retry_count = 0
        return self.make_embed(room_info, user_info)

    def _extract_live_data(self, html: str):
        """Try every known TikTok JSON data embedding pattern."""

        # Pattern 1: Next.js __NEXT_DATA__ (older TikTok layout)
        m = re.search(
            r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
            html, re.DOTALL,
        )
        if m:
            try:
                nd = json.loads(m.group(1))
                pp = nd["props"]["pageProps"]
                # TikTok has changed the key name several times
                room_info = (
                    pp.get("roomInfo")
                    or pp.get("liveRoom")
                    or (pp.get("roomInfoRes") or {}).get("data")
                    or (pp.get("data") or {}).get("liveRoom")
                    or {}
                )
                if room_info:
                    log.debug("TikTok: found room_info via __NEXT_DATA__")
                    return room_info, pp.get("userInfo") or {}
            except (json.JSONDecodeError, KeyError, TypeError) as exc:
                log.debug("TikTok: __NEXT_DATA__ parse error: %s", exc)

        # Pattern 2: SIGI_STATE (newer TikTok layout)
        m = re.search(r'<script id="SIGI_STATE"[^>]*>(.*?)</script>', html, re.DOTALL)
        if m:
            try:
                sigi = json.loads(m.group(1))
                lr_info = sigi.get("LiveRoom", {}).get("liveRoomUserInfo", {})
                room_info = lr_info.get("liveRoom") or {}
                if room_info:
                    log.debug("TikTok: found room_info via SIGI_STATE")
                    user_info = {"user": lr_info.get("user") or {}}
                    return room_info, user_info
            except (json.JSONDecodeError, KeyError, TypeError) as exc:
                log.debug("TikTok: SIGI_STATE parse error: %s", exc)

        # Pattern 3: __UNIVERSAL_DATA_FOR_REHYDRATION__ (newest TikTok layout)
        m = re.search(
            r'<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>(.*?)</script>',
            html, re.DOTALL,
        )
        if m:
            try:
                uni = json.loads(m.group(1))
                scope = uni.get("__DEFAULT_SCOPE__", {})
                live_detail = scope.get("webapp.live-detail", {})
                room_info = live_detail.get("liveRoom") or {}
                if room_info:
                    log.debug("TikTok: found room_info via __UNIVERSAL_DATA_FOR_REHYDRATION__")
                    user_info = live_detail.get("userInfo") or {}
                    return room_info, user_info
            except (json.JSONDecodeError, KeyError, TypeError) as exc:
                log.debug("TikTok: __UNIVERSAL_DATA_FOR_REHYDRATION__ parse error: %s", exc)

        log.debug("TikTok: no live data found in any known pattern")
        return {}, {}

    def make_embed(self, room_info: dict, user_info: dict):
        user = user_info.get("user") or {}
        stats = user_info.get("stats") or {}

        username = user.get("uniqueId") or self.name
        display_name = user.get("nickname") or username
        avatar = user.get("avatarThumb") or user.get("avatarMedium")

        title = room_info.get("title") or _("Untitled broadcast")
        url = f"https://www.tiktok.com/@{username}/live"
        embed = discord.Embed(title=title, url=url, color=0xFE2C55)
        embed.set_author(name=display_name)

        viewer_count = room_info.get("user_count") or room_info.get("userCount")
        if viewer_count is not None:
            embed.add_field(name=_("Viewers"), value=humanize_number(viewer_count))

        follower_count = stats.get("followerCount")
        if follower_count is not None:
            embed.add_field(name=_("Followers"), value=humanize_number(follower_count))

        if avatar:
            embed.set_thumbnail(url=avatar)

        cover = room_info.get("cover") or {}
        cover_urls = cover.get("url_list") or []
        if cover_urls:
            embed.set_image(url=rnd(cover_urls[0]))

        return embed
