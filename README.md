# Btmus Download Bot

Telegram bot for the Btmus 3.0.5 **bot-link import** ("Import via Download
Bot"). The app sends it a media link; the bot downloads the audio and
posts the MP3 to a channel, and the app picks it up from there.

## How it works

1. In the app you paste the bot's token once, paste a media link, and the
   app opens Telegram with the link ready to send to the bot. You tap Send.
2. This bot receives the link, downloads the audio, and posts the MP3 to
   your channel with `sendAudio` (title + performer set).
3. In the app you tap **Check for Returned Files** and import what the bot
   posted. Download All works on the returned files.

## Link handling — three cases

| Link | What the bot does |
|---|---|
| YouTube (`youtube.com`, `youtu.be`) | Downloads the video's audio with yt-dlp, converts to MP3 with ffmpeg, posts it to the channel. Download logic follows [ALGOANHAF/MP3](https://github.com/ALGOANHAF/MP3). A YouTube playlist link posts at most 20 tracks. |
| Spotify playlist / track (`open.spotify.com`) | Reads the track list with the Spotify API, then finds each track's audio on YouTube and downloads it — Spotify itself gives no audio. Approach follows [Masterolic/Spotify-Downloader](https://github.com/Masterolic/Spotify-Downloader). At most 50 tracks per playlist. Needs Spotify credentials (below); without them the bot replies that Spotify isn't set up yet. |
| Anything else | Replies in the private chat: *"Sorry, I can't do this link — I only handle YouTube links and Spotify playlists."* Nothing appears in the app. |

## Run it (Windows)

1. Install **Python 3.12** (python.org) and **ffmpeg** (gyan.dev builds —
   add it to PATH; check with `ffmpeg -version`).
2. Open a terminal in this folder:
   ```bat
   pip install -r requirements.txt
   copy .env.example .env
   ```
3. Open `.env` in Notepad and fill in:
   - `BOT_TOKEN` — from [@BotFather](https://t.me/BotFather) (`/newbot`).
     Use a **different bot** from the one the app uses for Telegram sync.
   - `CHANNEL_ID` — your channel, e.g. `@yourchannel` or `-1001234567890`.
     Create a channel, add the bot as admin, allow it to post messages.
   - `SPOTIFY_CLIENT_ID` / `SPOTIFY_CLIENT_SECRET` — only if you want
     Spotify links. Free from
     [developer.spotify.com](https://developer.spotify.com) (create an
     app, copy its Client ID and Client Secret). Leave empty to disable.
4. Run it:
   ```bat
   python bot.py
   ```
   Keep the window open — the bot must stay running to receive links.
   It logs every link it handles to the console. Press Ctrl+C to stop.

## Hosting on Render (free)

The bot can run on Render's free tier instead of a PC that stays on.
`render.yaml` (a Render Blueprint) is in this folder.

1. Push this folder to a GitHub repo.
2. In the Render dashboard: **New + → Blueprint** and point it at the
   repo. Render reads `render.yaml` and creates a free web service
   named `telegram-download-bot` (Frankfurt region).
3. When Render asks for the secret values, fill them in the dashboard:
   `BOT_TOKEN` and, if you want Spotify links, `SPOTIFY_CLIENT_ID` /
   `SPOTIFY_CLIENT_SECRET`. They are marked `sync: false` in
   `render.yaml`, so they are never stored in the repo. `CHANNEL_ID`
   is already set in the file (`-1004415561764`).
4. Render's free service sleeps after 15 minutes with no inbound
   traffic. Keep it awake with a free
   [UptimeRobot](https://uptimerobot.com) monitor pinging
   `https://telegram-download-bot.onrender.com/health` every 5 minutes
   (the bot answers `GET /health` with `200 ok`; anything else is 404).
5. Deploy, then send the bot a link in Telegram to check it works.

**Restart note (fast-forward).** Render's filesystem is ephemeral, so
`seen_updates.json` is lost on every redeploy or restart. On start the
bot polls `getUpdates` once and marks every update it can see as
already handled, without processing them — otherwise it would
re-download and re-post every recent link after each restart. The
trade-off: **a link sent while the bot is down is skipped and must be
re-sent** once the bot is back up.

## Important details

- **Polling, not webhook.** The bot polls `getUpdates` every 10 seconds
  (plus up to 2 seconds of random jitter) with **no offset** and
  remembers processed `update_id`s in `seen_updates.json`. This is
  deliberate: the app polls the same bot's `getUpdates`, so a webhook
  (which empties `getUpdates`) or polling with an offset (which consumes
  updates) would hide the returned files from the app. The 10-second
  interval also keeps the bot's requests from colliding with the app's
  (Telegram rejects concurrent `getUpdates` with 409). Do not "fix"
  this by adding a webhook or an offset.
- **The audio goes to the channel, never as a reply in the private
  chat.** A bot's own outgoing messages never appear in its `getUpdates`,
  so the app would never see a direct reply — only a channel post
  reaches it.
- **20 MB per-file cap.** The Telegram Bot API only lets the app fetch
  files up to 20 MB back (`getFile`), so files over 20 MB are skipped
  and the bot tells you in the chat. Most songs are well under this.
- **YouTube may block server IPs ("Sign in to confirm you're not a
  bot").** YouTube bot-checks datacenter IPs, so downloads from Render
  can fail even though the same link works in a browser. The bot first
  tries the `android_music` player client, then `android`, `ios`, and
  `web` — that fixes many videos, but not all. The reliable fix is a
  YouTube cookies file (see "YouTube cookies" below). The `yt-dlp`
  package can be upgraded with `pip install -U yt-dlp` if YouTube
  changes break downloads.

## YouTube cookies (when YouTube asks for sign-in)

When a link fails with "Sign in to confirm you're not a bot", the bot
needs your YouTube session cookies. They are **not** stored in the repo
— they travel as an environment variable, and only you can create them.

1. In Chrome or Edge, install the **"Get cookies.txt LOCALLY"**
   extension, then open https://www.youtube.com while logged in.
2. Click the extension and export the cookies for youtube.com. Save the
   file as `cookies.txt`.
3. Turn it into one line of base64 (Render env vars must be single-line).
   On Windows PowerShell, in the folder with the file:
   ```powershell
   [Convert]::ToBase64String([IO.File]::ReadAllBytes("cookies.txt"))
   ```
   Copy the long string it prints.
4. In Render, open the service → Environment → Add variable:
   key `YOUTUBE_COOKIES_B64`, value the string from step 3 → "Save,
   rebuild, and deploy".
5. Send the link to the bot again.

For a local/PC run, just put `cookies.txt` next to `bot.py` instead —
the bot picks it up automatically. Notes: cookies expire (refresh them
every few weeks), and they carry your YouTube login session, so keep
them private — never paste them into chat or commit them.
- **`/start`** replies with a one-line usage note. Other non-link text is
  ignored.
- Temp files are deleted after each link is handled.

## Tests

```bat
python -m unittest test_bot -v
```

Tests cover link classification (YouTube / Spotify playlist / track /
other), URL extraction, update-id dedup (including persistence across
restarts), the startup fast-forward, the `/health` endpoint, and the
exact reply texts. They need no credentials and make no network calls.
