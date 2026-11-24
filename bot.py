"""Telegram download bot for Btmus 3.0.5 (bot-link import).

The app side (already shipped in the 3.0.5 IPA) works like this:
  1. The user stores this bot's token ONCE in the app (iOS Keychain).
  2. The user pastes a media link in the app; the app opens Telegram with
     the link prefilled as a message to this bot, and the user taps Send.
  3. The user taps "Check for Returned Files" and the app polls this bot's
     getUpdates and imports any audio it finds.

This bot MUST fit that contract:
  - Post returned audio to a CHANNEL (bot as admin, can post) via
    sendAudio, with title and performer set. NEVER reply with the audio
    in the private chat: a bot's own outgoing messages never appear in
    its getUpdates, so the app would never see the file. Only a
    channel_post reaches the app's poll.
  - Poll getUpdates with raw HTTP calls and NO offset parameter, every
    few seconds, and dedupe by update_id locally (memory + a small JSON
    file so a restart doesn't reprocess). NEVER use a webhook (it makes
    getUpdates return nothing) and NEVER pass offset (it would consume
    updates the app still needs).

Link handling (Ahmed's spec):
  1. YouTube link -> yt-dlp audio download, ffmpeg -> MP3, post to channel.
     (Download logic of ALGOANHAF/MP3; NOT its polling loop.)
  2. Spotify playlist/track link -> Spotify API for the track list, then
     YouTube audio per track (Masterolic/Spotify-Downloader approach;
     NOT its Pyrogram transport/polling). Needs Spotify credentials.
  3. Anything else -> reply "Sorry, I can't do this link ...".

Config comes ONLY from environment variables (see .env.example):
  BOT_TOKEN, CHANNEL_ID, SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET.
No secrets are hardcoded anywhere in this file.

Run:  python bot.py     (after `pip install -r requirements.txt`)

Hosting: see README.md "Hosting on Render (free)". Render sets $PORT;
a tiny /health endpoint runs in a daemon thread so a free uptime
monitor can keep the service awake. On start the bot fast-forwards
past every update already visible in getUpdates (Render's disk is
ephemeral, so seen_updates.json is lost on restart); a link sent
while the bot is down must be re-sent.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import random
import re
import shutil
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import requests
import spotipy
from spotipy.oauth2 import SpotifyClientCredentials
from yt_dlp import YoutubeDL

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
CHANNEL_ID = os.environ.get("CHANNEL_ID", "").strip()
SPOTIFY_CLIENT_ID = os.environ.get("SPOTIFY_CLIENT_ID", "").strip()
SPOTIFY_CLIENT_SECRET = os.environ.get("SPOTIFY_CLIENT_SECRET", "").strip()

POLL_INTERVAL_SECONDS = 10         # Telegram rejects concurrent getUpdates
# calls with 409, and the app polls the same queue when the user taps
# "Check for Returned Files". A short interval meant the bot's request was
# often in flight at that exact moment, so the app's check failed with
# 409. 10s + jitter keeps the collision window tiny; a few seconds of
# extra latency per link is irrelevant for a download bot.
POLL_JITTER_SECONDS = 2            # random extra sleep per cycle, so the
# bot's polls never fall into a fixed rhythm the app's taps can align with
SEEN_FILE = Path(__file__).resolve().parent / "seen_updates.json"
MAX_SEEN_IDS = 2000                # cap the persisted dedup list
MAX_FILE_BYTES = 20 * 1024 * 1024  # Bot API getFile cap: app can't fetch bigger
MAX_YT_ENTRIES = 20                # cap tracks taken from one YouTube playlist
MAX_SPOTIFY_TRACKS = 50            # cap tracks taken from one Spotify playlist

YOUTUBE_HOSTS = {
    "youtube.com",
    "youtu.be",
    "music.youtube.com",
    "m.youtube.com",
}

CANT_REPLY = (
    "Sorry, I can't do this link — I only handle YouTube links "
    "and Spotify playlists."
)
START_TEXT = (
    "Send me a YouTube link or a Spotify playlist link and I'll post "
    "the MP3s to the channel for Btmus."
)
SPOTIFY_NOT_CONFIGURED = (
    "Spotify isn't set up on this bot yet — add the Spotify credentials "
    "and try again."
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)


def log(message: str) -> None:
    logging.info(message)


# --------------------------------------------------------------------------
# Pure logic: URL extraction + link classification (unit-tested)
# --------------------------------------------------------------------------

_URL_RE = re.compile(r"https?://[^\s<>\"]+")
_BARE_RE = re.compile(
    r"(?:www\.)?(?:youtube\.com|youtu\.be|open\.spotify\.com)/[^\s<>\"]+"
)


def extract_url(text: str) -> str | None:
    """First URL in *text*, or None if there is no link.

    Accepts full https:// URLs and bare youtube.com / youtu.be /
    open.spotify.com links (a scheme is added). Trailing punctuation
    from chat formatting (.,) etc.) is stripped.
    """
    if not text:
        return None
    match = _URL_RE.search(text)
    if match:
        return match.group(0).rstrip(").,;:!?")
    match = _BARE_RE.search(text)
    if match:
        return "https://" + match.group(0).rstrip(").,;:!?")
    return None


def classify_link(url: str) -> tuple[str, object | None]:
    """Classify a link.

    Returns:
      ("youtube", url)                          for youtube.com / youtu.be
      ("spotify", ("playlist"|"track", id))     for open.spotify.com links
      ("other", None)                           for anything else
    """
    try:
        parsed = urlparse(url)
    except ValueError:
        return ("other", None)
    host = (parsed.netloc or "").lower().split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    if host in YOUTUBE_HOSTS:
        return ("youtube", url)
    if host == "open.spotify.com":
        parts = [p for p in parsed.path.split("/") if p]
        if len(parts) >= 2 and parts[0] in ("playlist", "track"):
            return ("spotify", (parts[0], parts[1]))
    return ("other", None)


# --------------------------------------------------------------------------
# Update-id dedup (in memory + persisted JSON, so restarts don't reprocess)
# --------------------------------------------------------------------------


class SeenStore:
    """Set of processed update_ids, persisted to a JSON file."""

    def __init__(self, path: Path, max_size: int = MAX_SEEN_IDS):
        self.path = Path(path)
        self.max_size = max_size
        self.ids: set[int] = self._load()

    def _load(self) -> set[int]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return {int(x) for x in data if isinstance(x, int)}
        except (OSError, ValueError):
            return set()

    def save(self) -> None:
        try:
            ids = sorted(self.ids)[-self.max_size :]
            self.path.write_text(json.dumps(ids), encoding="utf-8")
        except OSError as exc:
            log(f"could not save seen ids: {exc}")

    def __contains__(self, update_id: int) -> bool:
        return update_id in self.ids

    def add(self, update_id: int) -> None:
        """Record an id and persist immediately, so a crash or restart
        never reprocesses it."""
        self.ids.add(update_id)
        if len(self.ids) > self.max_size:
            self.ids = set(sorted(self.ids)[-self.max_size :])
        self.save()


# --------------------------------------------------------------------------
# Raw Bot API client (no framework, so polling stays exactly as specified)
# --------------------------------------------------------------------------


class BotApiError(Exception):
    pass


class TelegramApi:
    def __init__(self, token: str):
        self.base = f"https://api.telegram.org/bot{token}"

    def _check(self, data: dict, what: str) -> dict:
        if not data.get("ok"):
            raise BotApiError(f"{what} failed: {data.get('description')}")
        return data

    def get_updates(self) -> list:
        """Poll getUpdates with NO offset.

        No offset means nothing is confirmed/consumed, so the app's own
        polling (which uses offset) still sees everything it needs.
        Only "message" updates are requested: the bot's own channel
        posts come back as channel_post and are not needed here.
        """
        resp = requests.get(
            f"{self.base}/getUpdates",
            params={"allowed_updates": json.dumps(["message"]), "limit": 100},
            timeout=30,
        )
        return self._check(resp.json(), "getUpdates").get("result", [])

    def send_text(self, chat_id: int | str, text: str) -> None:
        resp = requests.post(
            f"{self.base}/sendMessage",
            data={"chat_id": chat_id, "text": text},
            timeout=30,
        )
        self._check(resp.json(), "sendMessage")

    def send_audio(
        self, chat_id: int | str, path: Path, title: str, performer: str
    ) -> None:
        mime = _AUDIO_MIME_BY_SUFFIX.get(path.suffix.lower(), "audio/mpeg")
        with open(path, "rb") as audio_file:
            resp = requests.post(
                f"{self.base}/sendAudio",
                data={
                    "chat_id": chat_id,
                    "title": title,
                    "performer": performer,
                },
                files={"audio": (path.name, audio_file, mime)},
                timeout=180,
            )
        self._check(resp.json(), "sendAudio")


# The bot posts MP3s from yt-dlp, but the pytubefix fallback (see
# youtube_alt.py) can also post .m4a (AAC, iOS-native) or the original
# container when ffmpeg is missing. Telegram and the app accept any
# audio/* mime, so the content type follows the file's suffix.
_AUDIO_MIME_BY_SUFFIX = {
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".ogg": "audio/ogg",
    ".opus": "audio/ogg",
    ".webm": "audio/webm",
}


# --------------------------------------------------------------------------
# yt-dlp audio download (the ALGOANHAF/MP3 download logic)
# --------------------------------------------------------------------------


_COOKIEFILE_CACHE = {"resolved": False, "path": None}


def resolve_youtube_cookiefile() -> Path | None:
    """Locate the YouTube cookies file for yt-dlp, if one is configured.

    Two ways to provide it (first wins):
    1. YOUTUBE_COOKIES_B64 env var — base64 of a Netscape cookies.txt
       exported from a logged-in browser. Needed on Render, where env
       vars must be single-line and there is no persistent disk. The
       decoded file is written to the temp dir and reused.
    2. A cookies.txt file sitting next to bot.py (handy for local/PC runs).
    Returns None when neither is present. Never logs cookie contents.
    """
    if _COOKIEFILE_CACHE["resolved"]:
        return _COOKIEFILE_CACHE["path"]
    _COOKIEFILE_CACHE["resolved"] = True
    b64 = os.environ.get("YOUTUBE_COOKIES_B64", "").strip()
    if b64:
        try:
            data = base64.b64decode(b64, validate=True)
        except Exception as exc:
            log(f"YOUTUBE_COOKIES_B64 is not valid base64: {exc}")
            return None
        path = Path(tempfile.gettempdir()) / "btmu-youtube-cookies.txt"
        path.write_bytes(data)
        _COOKIEFILE_CACHE["path"] = path
        return path
    local = Path(__file__).resolve().parent / "cookies.txt"
    if local.is_file():
        _COOKIEFILE_CACHE["path"] = local
        return local
    return None


def youtube_failure_message(exc: Exception) -> str:
    """Turn a YouTube download error into a reply the user can act on."""
    text = str(exc)
    if "Sign in to confirm you" in text or "not a bot" in text:
        return (
            "YouTube refused that video — it wants a signed-in browser "
            "session (its bot check). The bot needs your YouTube cookies "
            "to download it. Ask Muse for the 2-minute cookie setup."
        )
    return f"Sorry, that YouTube link failed: {exc}"


def _ydl_opts(tmpdir: Path) -> dict:
    opts = {
        "format": "bestaudio/best",
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }
        ],
        "outtmpl": str(tmpdir / "%(id)s.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "noplaylist": False,
        # YouTube bot-checks the default "web" player client on datacenter
        # IPs (Render's included): "Sign in to confirm you're not a bot".
        # Verified 2026-11-23 on the failing video (Lady Gaga G.U.Y.):
        # web, android, tv_embedded, web_safari and web_music are all
        # bot-checked; ios and mweb extract but offer no audio format.
        # android_music is the one client that returns a usable audio
        # format for music videos, so it goes first; the others stay as
        # fallbacks. yt-dlp tries each in order.
        "extractor_args": {"youtube": {"player_client": ["android_music", "android", "ios", "web"]}},
    }
    cookiefile = resolve_youtube_cookiefile()
    if cookiefile is not None:
        opts["cookiefile"] = str(cookiefile)
    return opts


def _try_alt_download(video_url: str, tmpdir: Path) -> dict | None:
    """Fall back to the pytubefix downloader when yt-dlp fails.

    youtube_alt.py is a separate script (different package); it is
    imported lazily so the bot still runs when it is absent.
    """
    try:
        import youtube_alt
    except ImportError as exc:
        log(f"alt downloader unavailable: {exc}")
        return None
    try:
        return youtube_alt.download_video_audio(video_url, tmpdir)
    except Exception as exc:
        log(f"alt download failed for {video_url}: {exc}")
        return None


def download_video_as_mp3(video_url: str, tmpdir: Path) -> dict | None:
    """Download one YouTube video's audio as an MP3.

    Returns {"path", "title", "performer"} or None on failure.
    When yt-dlp fails (YouTube's bot-check), the pytubefix fallback is
    tried — it may return an .m4a instead of an .mp3, which the app
    also plays.
    """
    tmpdir = Path(tmpdir)
    before = set(tmpdir.glob("*.mp3"))
    try:
        with YoutubeDL(_ydl_opts(tmpdir)) as ydl:
            info = ydl.extract_info(video_url, download=False)
            if not info:
                return _try_alt_download(video_url, tmpdir)
            title = info.get("title") or "Unknown title"
            performer = (
                info.get("uploader") or info.get("artist") or "Unknown artist"
            )
            ydl.download([video_url])
    except Exception as exc:  # yt-dlp raises DownloadError and friends
        log(f"download failed for {video_url}: {exc}")
        return _try_alt_download(video_url, tmpdir)
    new_files = set(tmpdir.glob("*.mp3")) - before
    if not new_files:
        log(f"no MP3 produced for {video_url}")
        return _try_alt_download(video_url, tmpdir)
    path = max(new_files, key=lambda p: p.stat().st_mtime)
    return {"path": path, "title": title, "performer": performer}


def download_audio_files(url: str, tmpdir: Path) -> list[dict]:
    """Download audio from a YouTube URL (video or playlist) as MP3s.

    A playlist URL yields at most MAX_YT_ENTRIES tracks, in order.
    When yt-dlp cannot even list the URL (bot-check on the playlist
    page), the pytubefix fallback lists and downloads it instead.
    """
    tmpdir = Path(tmpdir)
    try:
        with YoutubeDL(_ydl_opts(tmpdir)) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as exc:
        log(f"YouTube list failed for {url}: {exc}; trying alt downloader")
        try:
            import youtube_alt
        except ImportError as exc:
            log(f"alt downloader unavailable: {exc}")
            return []
        return youtube_alt.download_audio_files(url, tmpdir)
    if not info:
        return []
    entries = info.get("entries")
    if entries:
        videos = [e for e in entries if e][:MAX_YT_ENTRIES]
    else:
        videos = [info]
    results = []
    for video in videos:
        video_url = video.get("webpage_url") or video.get("url")
        if not video_url:
            continue
        # Playlist entries may only carry an id; rebuild the watch URL.
        if video_url == video.get("id"):
            video_url = f"https://www.youtube.com/watch?v={video['id']}"
        result = download_video_as_mp3(video_url, tmpdir)
        if result is not None:
            results.append(result)
    return results


def youtube_search_first(query: str, tmpdir: Path) -> dict | None:
    """First YouTube result for *query*, or None if nothing found."""
    try:
        with YoutubeDL(_ydl_opts(Path(tmpdir))) as ydl:
            info = ydl.extract_info(f"ytsearch1:{query}", download=False)
    except Exception as exc:
        log(f"YouTube search failed for {query!r}: {exc}")
        return None
    entries = (info or {}).get("entries") or []
    if not entries:
        return None
    return entries[0]


# --------------------------------------------------------------------------
# Spotify (the Masterolic/Spotify-Downloader approach: metadata -> YouTube)
# --------------------------------------------------------------------------


def spotify_client() -> spotipy.Spotify | None:
    """Spotify client (client-credentials flow) or None if not configured."""
    if not SPOTIFY_CLIENT_ID or not SPOTIFY_CLIENT_SECRET:
        return None
    auth = SpotifyClientCredentials(
        client_id=SPOTIFY_CLIENT_ID, client_secret=SPOTIFY_CLIENT_SECRET
    )
    return spotipy.Spotify(auth_manager=auth)


def _track_info(track: dict) -> dict:
    artists = ", ".join(a.get("name", "") for a in track.get("artists", []))
    return {"name": track.get("name") or "Unknown", "artists": artists}


def fetch_spotify_tracks(
    sp: spotipy.Spotify, kind: str, item_id: str
) -> list[dict]:
    """Track list for a Spotify playlist (paged, capped) or a single track."""
    if kind == "track":
        return [_track_info(sp.track(item_id))]
    tracks: list[dict] = []
    results = sp.playlist_tracks(item_id, limit=100)
    while results:
        for item in results.get("items", []):
            track = item.get("track")
            if track:
                tracks.append(_track_info(track))
            if len(tracks) >= MAX_SPOTIFY_TRACKS:
                return tracks
        results = sp.next(results) if results.get("next") else None
    return tracks


# --------------------------------------------------------------------------
# Sending results to the channel
# --------------------------------------------------------------------------


def post_files_to_channel(
    api: TelegramApi,
    files: list[dict],
    user_chat_id: int | str,
    channel_id: str,
) -> int:
    """Post MP3s to the channel; returns how many were posted.

    Files over the Bot API 20 MB cap are skipped (the app could not fetch
    them back) and the user is told in the private chat.
    """
    posted = 0
    for item in files:
        path = item["path"]
        if path.stat().st_size > MAX_FILE_BYTES:
            api.send_text(
                user_chat_id,
                f"Skipped “{item['title']}” — the file is over 20 MB, "
                "too big for the app to fetch from Telegram.",
            )
            continue
        api.send_audio(channel_id, path, item["title"], item["performer"])
        log(f"posted to channel: {item['title']} ({path.stat().st_size} bytes)")
        posted += 1
    return posted


# --------------------------------------------------------------------------
# Link handlers
# --------------------------------------------------------------------------


def handle_youtube(
    api: TelegramApi, url: str, user_chat_id: int | str, channel_id: str
) -> None:
    log(f"YouTube link: {url}")
    tmpdir = Path(tempfile.mkdtemp(prefix="btmu-yt-"))
    try:
        try:
            files = download_audio_files(url, tmpdir)
        except Exception as exc:
            log(f"YouTube download failed: {exc}")
            api.send_text(user_chat_id, youtube_failure_message(exc))
            return
        if not files:
            api.send_text(
                user_chat_id,
                "I couldn't download any audio from that YouTube link.",
            )
            return
        posted = post_files_to_channel(api, files, user_chat_id, channel_id)
        if posted:
            api.send_text(
                user_chat_id,
                f"Done — {posted} file(s) posted to the channel. "
                "Tap “Check for Returned Files” in Btmus to import.",
            )
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def handle_spotify(
    api: TelegramApi,
    detail: tuple[str, str],
    user_chat_id: int | str,
    channel_id: str,
) -> None:
    kind, item_id = detail
    sp = spotify_client()
    if sp is None:
        api.send_text(user_chat_id, SPOTIFY_NOT_CONFIGURED)
        return
    log(f"Spotify {kind}: {item_id}")
    try:
        tracks = fetch_spotify_tracks(sp, kind, item_id)
    except Exception as exc:
        log(f"Spotify fetch failed: {exc}")
        api.send_text(
            user_chat_id, f"Sorry, I couldn't read that Spotify {kind}: {exc}"
        )
        return
    if not tracks:
        api.send_text(
            user_chat_id, "That Spotify link has no tracks I can use."
        )
        return
    tmpdir = Path(tempfile.mkdtemp(prefix="btmu-sp-"))
    try:
        posted = 0
        missed = 0
        for track in tracks:
            query = (
                f"{track['artists']} - {track['name']}"
                if track["artists"]
                else track["name"]
            )
            found = youtube_search_first(query, tmpdir)
            if not found:
                log(f"no YouTube match for {query!r}")
                missed += 1
                continue
            video_url = found.get("webpage_url") or (
                f"https://www.youtube.com/watch?v={found['id']}"
                if found.get("id")
                else None
            )
            if not video_url:
                missed += 1
                continue
            result = download_video_as_mp3(video_url, tmpdir)
            if result is None:
                missed += 1
                continue
            # Use the Spotify metadata for the name, not the video title.
            result["title"] = track["name"]
            result["performer"] = track["artists"] or "Unknown artist"
            posted += post_files_to_channel(
                api, [result], user_chat_id, channel_id
            )
        if posted:
            summary = f"Done — {posted} file(s) posted to the channel."
            if missed:
                summary += f" {missed} track(s) had no match and were skipped."
            summary += " Tap “Check for Returned Files” in Btmus to import."
            api.send_text(user_chat_id, summary)
        else:
            api.send_text(
                user_chat_id,
                "I couldn't find audio for any track in that playlist.",
            )
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def handle_message(
    api: TelegramApi, msg: dict, channel_id: str
) -> None:
    """Route one incoming message. Only link messages get a reply."""
    text = (msg.get("text") or "").strip()
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    sender = msg.get("from") or {}
    if sender.get("is_bot") or chat_id is None or not text:
        return
    if text.startswith("/start"):
        api.send_text(chat_id, START_TEXT)
        return
    url = extract_url(text)
    if not url:
        return  # ignore non-link text
    kind, detail = classify_link(url)
    if kind == "youtube":
        handle_youtube(api, url, chat_id, channel_id)
    elif kind == "spotify":
        handle_spotify(api, detail, chat_id, channel_id)
    else:
        api.send_text(chat_id, CANT_REPLY)


# --------------------------------------------------------------------------
# Health endpoint + startup fast-forward (Render free tier)
# --------------------------------------------------------------------------


class HealthHandler(BaseHTTPRequestHandler):
    """GET /health -> 200 "ok", anything else -> 404."""

    def do_GET(self):
        if self.path == "/health":
            body = b"ok"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

    def log_message(self, *args):
        pass  # keep the console clean


def start_health_server(port: int | None = None) -> ThreadingHTTPServer:
    """Serve GET /health on 0.0.0.0:$PORT in a daemon thread.

    Render's free web service sleeps after 15 minutes with no inbound
    HTTP traffic, so a free uptime monitor (e.g. UptimeRobot) pings
    /health to keep the bot awake. Render sets the PORT env var.
    Stdlib only, no new dependencies.
    """
    if port is None:
        port = int(os.environ.get("PORT", "10000"))
    server = ThreadingHTTPServer(("0.0.0.0", port), HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    log(f"health endpoint listening on :{port} (GET /health)")
    return server


def fast_forward_seen(api: TelegramApi, seen: SeenStore) -> int:
    """Mark every update currently visible in getUpdates as seen,
    WITHOUT processing any of them.

    Render's filesystem is ephemeral, so seen_updates.json is lost on
    every redeploy/restart. Without this, the bot would re-download
    and re-post every recent link after each restart. A link sent
    while the bot is down is skipped by this fast-forward and must
    be re-sent. Returns how many update_ids were recorded.
    """
    try:
        updates = api.get_updates()
    except Exception as exc:
        log(f"fast-forward skipped (getUpdates failed): {exc}")
        return 0
    count = 0
    for update in updates:
        update_id = update.get("update_id")
        if update_id is not None and update_id not in seen:
            seen.add(update_id)  # add() persists immediately
            count += 1
    log(f"fast-forward: marked {count} update(s) as seen")
    return count


# --------------------------------------------------------------------------
# Main loop
# --------------------------------------------------------------------------


def load_config() -> tuple[str, str]:
    token = BOT_TOKEN
    channel = CHANNEL_ID
    if not token:
        raise SystemExit(
            "BOT_TOKEN is not set. Copy .env.example to .env and fill it in."
        )
    if not channel:
        raise SystemExit(
            "CHANNEL_ID is not set. Copy .env.example to .env and fill it in."
        )
    return token, channel


def main() -> int:
    start_health_server()  # up before anything else, so Render's
    # health check passes as soon as the process starts
    token, channel = load_config()
    cookiefile = resolve_youtube_cookiefile()
    log(
        "YouTube cookies: configured"
        if cookiefile is not None
        else "YouTube cookies: not configured (some videos may ask for sign-in)"
    )
    log(
        "ffmpeg: available"
        if shutil.which("ffmpeg")
        else "ffmpeg: NOT found (opus fallback will post the original file)"
    )
    api = TelegramApi(token)
    try:
        me = requests.get(f"{api.base}/getMe", timeout=15).json()
    except requests.RequestException as exc:
        raise SystemExit(f"Could not reach Telegram: {exc}")
    if not me.get("ok"):
        raise SystemExit(f"Bad BOT_TOKEN: {me.get('description')}")
    log(f"logged in as @{me['result'].get('username')} — polling getUpdates")
    seen = SeenStore(SEEN_FILE)
    fast_forward_seen(api, seen)
    while True:
        try:
            updates = api.get_updates()
        except BotApiError as exc:
            if "Unauthorized" in str(exc):
                log(f"FATAL: {exc} — check BOT_TOKEN")
                return 1
            log(f"getUpdates error: {exc}; retrying in 10s")
            time.sleep(10)
            continue
        except requests.RequestException as exc:
            log(f"network error: {exc}; retrying in 5s")
            time.sleep(5)
            continue
        for update in updates:
            update_id = update.get("update_id")
            if update_id is None or update_id in seen:
                continue
            seen.add(update_id)  # add() persists immediately
            message = update.get("message")
            if message:
                try:
                    handle_message(api, message, channel)
                except Exception as exc:  # one bad message must not kill the bot
                    log(f"handler error for update {update_id}: {exc}")
        time.sleep(POLL_INTERVAL_SECONDS + random.uniform(0, POLL_JITTER_SECONDS))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
