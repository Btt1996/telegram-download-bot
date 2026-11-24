"""Alternative YouTube audio downloader using pytubefix (not yt-dlp).

Why this exists: yt-dlp gets "Sign in to confirm you're not a bot"
from YouTube on datacenter IPs (Render's included), and the
player-client shuffle doesn't fix it for every video. pytubefix talks
to the same InnerTube API with a different request pattern and
different clients, and it got through for the exact video yt-dlp
consistently fails on (Lady Gaga G.U.Y., ohs0a-QnFF4 — downloaded in
full 2026-11-23). YouTube's bot-check is intermittent, so this retries
across clients (ANDROID, IOS, TV, WEB) before giving up.

Standalone use:
    python youtube_alt.py <youtube-url> [outdir]

As a library it mirrors bot.py's download functions, so the bot falls
back to it when yt-dlp fails:
    download_video_audio(url, tmpdir) -> {"path", "title", "performer"} | None
    download_audio_files(url, tmpdir) -> list[dict]

Format: prefers an AAC stream (audio/mp4, itag 140) saved as .m4a —
iOS plays AAC natively, so no conversion is needed. Otherwise takes
the best audio (usually opus/webm) and converts it to MP3 with ffmpeg
when ffmpeg is installed; without ffmpeg the original file is returned
as-is.
"""

from __future__ import annotations

import logging
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from pytubefix import Playlist, YouTube

# pytubefix downloads with urllib and no timeout of its own, so a
# stalled media connection could hang the bot for many minutes. The
# global socket timeout makes stalled downloads fail fast instead.
socket.setdefaulttimeout(120)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)


def log(message: str) -> None:
    logger.info(message)


# Clients to try, in order. ANDROID/IOS/TV worked first-try on the
# failing video; WEB needed a retry. MWEB is skipped (flaky syntax
# errors in the sweep).
CLIENTS = ("ANDROID", "IOS", "TV", "WEB")
ATTEMPTS_PER_CLIENT = 2
RETRY_DELAY_SECONDS = 1.0

MAX_YT_ENTRIES = 20  # same cap as bot.py: at most 20 tracks per playlist


def _open_video(url: str, youtube_cls=YouTube) -> object:
    """Open a video, retrying across clients.

    Raises the last error seen when every client and attempt fails.
    """
    last_exc: Exception | None = None
    for client in CLIENTS:
        for attempt in range(1, ATTEMPTS_PER_CLIENT + 1):
            try:
                yt = youtube_cls(url, client=client)
                # Touching .title forces the fetch; a bot-check raises here.
                _ = yt.title
                return yt
            except Exception as exc:  # BotDetection and friends
                last_exc = exc
                log(f"alt: {client} attempt {attempt} failed: {exc}")
                time.sleep(RETRY_DELAY_SECONDS)
    assert last_exc is not None
    raise last_exc


def _pick_audio_stream(yt) -> tuple[object, bool]:
    """Best audio stream: AAC first, else best audio overall.

    Returns (stream, is_aac). Raises ValueError when no audio exists.
    """
    audio = yt.streams.filter(only_audio=True)
    aac = audio.filter(mime_type="audio/mp4").order_by("abr").desc().first()
    if aac is not None:
        return aac, True
    best = audio.order_by("abr").desc().first()
    if best is None:
        raise ValueError("no audio streams")
    return best, False


def _download_stream(stream, tmpdir: Path, filename: str) -> Path:
    """Download a pytubefix stream to *tmpdir/filename*.

    Uses pytubefix's own download() because YouTube's SABR stream URLs
    need the exact request it builds — a plain requests GET comes back
    with a 31-byte `sabr.malformed_config` error instead of audio.
    A failed or empty download leaves no partial file behind.
    """
    dest = tmpdir / filename
    try:
        downloaded = Path(
            stream.download(output_path=str(tmpdir), filename=filename)
        )
    except Exception:
        dest.unlink(missing_ok=True)
        raise
    if not downloaded.is_file() or downloaded.stat().st_size == 0:
        downloaded.unlink(missing_ok=True)
        raise ValueError("download produced an empty file")
    return downloaded


def _to_mp3(src: Path) -> Path | None:
    """Convert *src* to MP3 with ffmpeg, or None when ffmpeg is missing."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        log("alt: ffmpeg not found; keeping original format")
        return None
    dst = src.with_suffix(".mp3")
    result = subprocess.run(
        [
            ffmpeg,
            "-y",
            "-v",
            "error",
            "-i",
            str(src),
            "-vn",
            "-c:a",
            "libmp3lame",
            "-b:a",
            "192k",
            str(dst),
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 or not dst.is_file():
        log(f"alt: ffmpeg conversion failed: {result.stderr.strip()}")
        return None
    src.unlink(missing_ok=True)
    return dst


def download_video_audio(video_url: str, tmpdir: Path) -> dict | None:
    """Download one YouTube video's audio.

    Returns {"path", "title", "performer"} or None on failure.
    The path is .m4a (AAC) when available, else .mp3 (ffmpeg) or the
    original container (opus/webm) when ffmpeg is missing.
    """
    tmpdir = Path(tmpdir)
    try:
        yt = _open_video(video_url)
        stream, is_aac = _pick_audio_stream(yt)
        stem = getattr(yt, "video_id", None) or "audio"
        if is_aac:
            filename = f"{stem}.m4a"
        else:
            filename = f"{stem}.{stream.subtype}"
        downloaded = _download_stream(stream, tmpdir, filename)
        path = downloaded
        if not is_aac:
            converted = _to_mp3(downloaded)
            if converted is not None:
                path = converted
        title = getattr(yt, "title", None) or "Unknown title"
        performer = getattr(yt, "author", None) or "Unknown artist"
        return {"path": path, "title": title, "performer": performer}
    except Exception as exc:
        log(f"alt download failed for {video_url}: {exc}")
        return None


def download_audio_files(url: str, tmpdir: Path) -> list[dict]:
    """Download audio from a YouTube URL (video or playlist).

    A playlist URL yields at most MAX_YT_ENTRIES tracks, in order.
    """
    tmpdir = Path(tmpdir)
    if "list=" in url:
        try:
            playlist = Playlist(url)
            video_urls = list(playlist.video_urls)[:MAX_YT_ENTRIES]
        except Exception as exc:
            log(f"alt playlist failed for {url}: {exc}")
            return []
        results = []
        for video_url in video_urls:
            result = download_video_audio(video_url, tmpdir)
            if result is not None:
                results.append(result)
        return results
    result = download_video_audio(url, tmpdir)
    return [result] if result is not None else []


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: python youtube_alt.py <youtube-url> [outdir]")
        return 2
    url = argv[1]
    outdir = Path(argv[2]) if len(argv) > 2 else Path(tempfile.mkdtemp(prefix="btmu-alt-"))
    outdir.mkdir(parents=True, exist_ok=True)
    files = download_audio_files(url, outdir)
    if not files:
        print("FAILED: no audio downloaded")
        return 1
    for item in files:
        print(f"{item['path']} | {item['title']} | {item['performer']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
