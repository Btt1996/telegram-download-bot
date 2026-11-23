"""Unit tests for the Telegram download bot's pure logic.

No credentials, no network calls. Run from this folder with:
    python -m unittest test_bot -v
"""

import base64
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.request
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bot
from bot import (
    CANT_REPLY,
    START_TEXT,
    SeenStore,
    _ydl_opts,
    classify_link,
    extract_url,
    fast_forward_seen,
    resolve_youtube_cookiefile,
    start_health_server,
    youtube_failure_message,
)


class TestExtractUrl(unittest.TestCase):
    def test_full_url_in_text(self):
        self.assertEqual(
            extract_url("here it is https://www.youtube.com/watch?v=abc123 thanks"),
            "https://www.youtube.com/watch?v=abc123",
        )

    def test_trailing_punctuation_stripped(self):
        self.assertEqual(
            extract_url("check this: https://youtu.be/abc123."),
            "https://youtu.be/abc123",
        )

    def test_bare_youtube_link_gets_scheme(self):
        self.assertEqual(
            extract_url("youtube.com/watch?v=abc123"),
            "https://youtube.com/watch?v=abc123",
        )

    def test_plain_text_has_no_url(self):
        self.assertIsNone(extract_url("hello there"))
        self.assertIsNone(extract_url(""))
        self.assertIsNone(extract_url("   "))


class TestClassifyLink(unittest.TestCase):
    def test_youtube_watch(self):
        kind, detail = classify_link("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
        self.assertEqual(kind, "youtube")

    def test_youtube_short_link(self):
        kind, _ = classify_link("https://youtu.be/dQw4w9WgXcQ")
        self.assertEqual(kind, "youtube")

    def test_youtube_music(self):
        kind, _ = classify_link("https://music.youtube.com/watch?v=dQw4w9WgXcQ")
        self.assertEqual(kind, "youtube")

    def test_youtube_playlist(self):
        kind, _ = classify_link(
            "https://www.youtube.com/playlist?list=PL1234567890abcdef"
        )
        self.assertEqual(kind, "youtube")

    def test_spotify_playlist(self):
        kind, detail = classify_link(
            "https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M"
        )
        self.assertEqual(kind, "spotify")
        self.assertEqual(detail, ("playlist", "37i9dQZF1DXcBWIGoYBM5M"))

    def test_spotify_playlist_query_stripped(self):
        kind, detail = classify_link(
            "https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M?si=abc123"
        )
        self.assertEqual(detail, ("playlist", "37i9dQZF1DXcBWIGoYBM5M"))

    def test_spotify_track(self):
        kind, detail = classify_link(
            "https://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQC"
        )
        self.assertEqual(kind, "spotify")
        self.assertEqual(detail, ("track", "4uLU6hMCjMI75M1A2tKUQC"))

    def test_spotify_album_is_other(self):
        kind, _ = classify_link("https://open.spotify.com/album/abc123")
        self.assertEqual(kind, "other")

    def test_soundcloud_is_other(self):
        kind, _ = classify_link("https://soundcloud.com/artist/track")
        self.assertEqual(kind, "other")

    def test_direct_mp3_is_other(self):
        kind, _ = classify_link("https://example.com/song.mp3")
        self.assertEqual(kind, "other")

    def test_garbage_is_other(self):
        kind, _ = classify_link("not a url")
        self.assertEqual(kind, "other")


class TestSeenStore(unittest.TestCase):
    def test_add_and_contains(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SeenStore(Path(tmp) / "seen.json")
            self.assertNotIn(42, store)
            store.add(42)
            self.assertIn(42, store)
            self.assertNotIn(43, store)

    def test_persists_across_instances(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "seen.json"
            SeenStore(path).add(7)
            SeenStore(path).add(8)
            fresh = SeenStore(path)
            self.assertIn(7, fresh)
            self.assertIn(8, fresh)
            self.assertNotIn(9, fresh)

    def test_corrupt_file_starts_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "seen.json"
            path.write_text("this is not json", encoding="utf-8")
            store = SeenStore(path)
            self.assertNotIn(1, store)
            store.add(1)
            self.assertIn(1, store)

    def test_max_size_trims_oldest(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SeenStore(Path(tmp) / "seen.json", max_size=3)
            for uid in (1, 2, 3, 4, 5):
                store.add(uid)
            self.assertNotIn(1, store)
            self.assertNotIn(2, store)
            self.assertIn(3, store)
            self.assertIn(4, store)
            self.assertIn(5, store)

    def test_save_writes_valid_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "seen.json"
            store = SeenStore(path)
            store.add(10)
            store.add(20)
            store.save()
            self.assertEqual(sorted(json.loads(path.read_text())), [10, 20])


class TestFastForward(unittest.TestCase):
    def test_marks_visible_updates_without_processing(self):
        class FakeApi:
            def __init__(self):
                self.called = 0

            def get_updates(self):
                self.called += 1
                return [
                    {"update_id": 11, "message": {"text": "link"}},
                    {"update_id": 12, "message": {"text": "link"}},
                ]

        with tempfile.TemporaryDirectory() as tmp:
            store = SeenStore(Path(tmp) / "seen.json")
            api = FakeApi()
            self.assertEqual(fast_forward_seen(api, store), 2)
            self.assertEqual(api.called, 1)
            self.assertIn(11, store)
            self.assertIn(12, store)
            # A second fast-forward sees the same updates and records none.
            self.assertEqual(fast_forward_seen(api, store), 0)

    def test_get_updates_failure_returns_zero(self):
        class FailingApi:
            def get_updates(self):
                raise RuntimeError("network down")

        with tempfile.TemporaryDirectory() as tmp:
            store = SeenStore(Path(tmp) / "seen.json")
            self.assertEqual(fast_forward_seen(FailingApi(), store), 0)


class TestHealthEndpoint(unittest.TestCase):
    def test_health_returns_ok_and_other_paths_404(self):
        server = start_health_server(port=0)  # OS-assigned port
        port = server.server_address[1]
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/health", timeout=5
            ) as resp:
                self.assertEqual(resp.status, 200)
                self.assertEqual(resp.read(), b"ok")
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/nope", timeout=5
                )
            self.assertEqual(ctx.exception.code, 404)
        finally:
            server.shutdown()
            server.server_close()


class TestYoutubeCookies(unittest.TestCase):
    def setUp(self):
        self._old_env = os.environ.pop("YOUTUBE_COOKIES_B64", None)
        self._reset_cache()

    def tearDown(self):
        if self._old_env is None:
            os.environ.pop("YOUTUBE_COOKIES_B64", None)
        else:
            os.environ["YOUTUBE_COOKIES_B64"] = self._old_env
        self._reset_cache()

    @staticmethod
    def _reset_cache():
        bot._COOKIEFILE_CACHE["resolved"] = False
        bot._COOKIEFILE_CACHE["path"] = None

    def test_b64_env_var_decodes_to_file(self):
        content = "# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tFALSE\t0\tSID\txyz\n"
        os.environ["YOUTUBE_COOKIES_B64"] = base64.b64encode(
            content.encode()
        ).decode()
        path = resolve_youtube_cookiefile()
        self.assertIsNotNone(path)
        self.assertEqual(Path(path).read_text(), content)
        # Memoized: a second call returns the same path.
        self.assertEqual(resolve_youtube_cookiefile(), path)

    def test_invalid_b64_returns_none(self):
        os.environ["YOUTUBE_COOKIES_B64"] = "!!!not-base64!!!"
        self.assertIsNone(resolve_youtube_cookiefile())

    def test_no_config_returns_none(self):
        # No env var and no cookies.txt next to bot.py.
        self.assertIsNone(resolve_youtube_cookiefile())

    def test_ydl_opts_includes_cookiefile_when_configured(self):
        content = "# Netscape HTTP Cookie File\n"
        os.environ["YOUTUBE_COOKIES_B64"] = base64.b64encode(
            content.encode()
        ).decode()
        with tempfile.TemporaryDirectory() as tmp:
            opts = _ydl_opts(Path(tmp))
        self.assertIn("cookiefile", opts)
        self.assertEqual(Path(opts["cookiefile"]).read_text(), content)

    def test_ydl_opts_omits_cookiefile_when_not_configured(self):
        with tempfile.TemporaryDirectory() as tmp:
            opts = _ydl_opts(Path(tmp))
        self.assertNotIn("cookiefile", opts)

    def test_player_clients_start_with_android_music(self):
        with tempfile.TemporaryDirectory() as tmp:
            opts = _ydl_opts(Path(tmp))
        clients = opts["extractor_args"]["youtube"]["player_client"]
        self.assertEqual(clients[0], "android_music")
        self.assertIn("web", clients)


class TestYoutubeFailureMessage(unittest.TestCase):
    def test_bot_check_points_at_cookies(self):
        msg = youtube_failure_message(
            Exception(
                "ERROR: [youtube] abc: Sign in to confirm you're not a bot."
            )
        )
        self.assertIn("cookies", msg)
        self.assertNotIn("Sign in to confirm", msg)

    def test_other_errors_pass_through(self):
        msg = youtube_failure_message(Exception("some network error"))
        self.assertTrue(msg.startswith("Sorry, that YouTube link failed:"))
        self.assertIn("some network error", msg)


class TestReplyTexts(unittest.TestCase):
    def test_cant_reply_exact(self):
        self.assertEqual(
            CANT_REPLY,
            "Sorry, I can't do this link — I only handle YouTube links "
            "and Spotify playlists.",
        )

    def test_start_text_is_one_line(self):
        self.assertTrue(START_TEXT.strip())
        self.assertNotIn("\n", START_TEXT.strip())


if __name__ == "__main__":
    unittest.main()
