"""Unit tests for youtube_alt.py (the pytubefix downloader).

No credentials, no network calls. Run from this folder with:
    python -m unittest test_youtube_alt -v
"""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import youtube_alt
from youtube_alt import (
    _download_stream,
    _open_video,
    _pick_audio_stream,
    _to_mp3,
    download_audio_files,
    download_video_audio,
)


class FakeStream:
    def __init__(self, itag, mime_type, abr, subtype, content=b"FAKEAUDIO"):
        self.itag = itag
        self.mime_type = mime_type
        self.abr = abr
        self.subtype = subtype
        self._content = content
        self.download_calls = []

    def download(self, output_path, filename):
        path = Path(output_path) / filename
        path.write_bytes(self._content)
        self.download_calls.append(path)
        return str(path)


class FakeQuery:
    def __init__(self, streams):
        self._streams = list(streams)

    def filter(self, **kwargs):
        result = self._streams
        if kwargs.get("mime_type"):
            result = [s for s in result if s.mime_type == kwargs["mime_type"]]
        return FakeQuery(result)

    def order_by(self, _key):
        return self

    def desc(self):
        return self

    def first(self):
        if not self._streams:
            return None
        return max(self._streams, key=lambda s: int(s.abr.replace("kbps", "")))


class FakeYouTube:
    def __init__(self, streams, title="Song", author="Artist", video_id="abc123"):
        self._streams = streams
        self.title = title
        self.author = author
        self.video_id = video_id

    @property
    def streams(self):
        return FakeQuery(self._streams)


def _streams():
    return [
        FakeStream(251, "audio/webm", "160kbps", "webm"),
        FakeStream(140, "audio/mp4", "128kbps", "mp4"),
        FakeStream(139, "audio/mp4", "48kbps", "mp4"),
    ]


class TestPickAudioStream(unittest.TestCase):
    def test_prefers_aac_over_higher_bitrate_opus(self):
        stream, is_aac = _pick_audio_stream(FakeYouTube(_streams()))
        self.assertTrue(is_aac)
        self.assertEqual(stream.itag, 140)

    def test_best_audio_when_no_aac(self):
        streams = [s for s in _streams() if s.mime_type != "audio/mp4"]
        stream, is_aac = _pick_audio_stream(FakeYouTube(streams))
        self.assertFalse(is_aac)
        self.assertEqual(stream.itag, 251)

    def test_no_audio_raises(self):
        with self.assertRaises(ValueError):
            _pick_audio_stream(FakeYouTube([]))


class TestOpenVideoRetries(unittest.TestCase):
    def test_retries_across_clients_then_succeeds(self):
        calls = []

        def flaky_cls(url, client):
            calls.append(client)
            if len(calls) < 3:
                raise RuntimeError("detected as a bot")
            return FakeYouTube(_streams())

        yt = _open_video("https://www.youtube.com/watch?v=x", youtube_cls=flaky_cls)
        self.assertEqual(yt.title, "Song")
        self.assertEqual(calls, ["ANDROID", "ANDROID", "IOS"])

    def test_raises_after_all_clients_exhausted(self):
        def always_fail(url, client):
            raise RuntimeError("detected as a bot")

        with self.assertRaises(RuntimeError):
            _open_video("https://www.youtube.com/watch?v=x", youtube_cls=always_fail)


class TestDownloadStream(unittest.TestCase):
    def test_download_writes_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _download_stream(
                FakeStream(140, "audio/mp4", "128kbps", "mp4"),
                Path(tmp),
                "abc123.m4a",
            )
            self.assertEqual(path.read_bytes(), b"FAKEAUDIO")

    def test_failed_download_leaves_no_partial_file(self):
        class BrokenStream(FakeStream):
            def download(self, output_path, filename):
                path = Path(output_path) / filename
                path.write_bytes(b"partial")
                raise RuntimeError("connection dropped")

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(RuntimeError):
                _download_stream(
                    BrokenStream(140, "audio/mp4", "128kbps", "mp4"),
                    Path(tmp),
                    "abc123.m4a",
                )
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_empty_download_is_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                _download_stream(
                    FakeStream(140, "audio/mp4", "128kbps", "mp4", content=b""),
                    Path(tmp),
                    "abc123.m4a",
                )
            self.assertEqual(list(Path(tmp).iterdir()), [])


class TestToMp3(unittest.TestCase):
    def test_no_ffmpeg_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "song.webm"
            src.write_bytes(b"data")
            with mock.patch("youtube_alt.shutil.which", return_value=None):
                self.assertIsNone(_to_mp3(src))
            self.assertTrue(src.is_file())

    def test_converts_opus_to_mp3_with_real_ffmpeg(self):
        import subprocess

        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "tone.opus"
            subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-v",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "sine=frequency=440:duration=1",
                    "-c:a",
                    "libopus",
                    str(src),
                ],
                check=True,
            )
            out = _to_mp3(src)
            self.assertIsNotNone(out)
            self.assertEqual(out.suffix, ".mp3")
            self.assertGreater(out.stat().st_size, 0)
            self.assertFalse(src.exists())


class TestDownloadVideoAudio(unittest.TestCase):
    def test_aac_path_returns_m4a(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch(
                "youtube_alt._open_video",
                return_value=FakeYouTube(_streams()),
            ):
                result = download_video_audio(
                    "https://www.youtube.com/watch?v=abc123", Path(tmp)
                )
        self.assertIsNotNone(result)
        self.assertEqual(result["path"].suffix, ".m4a")
        self.assertEqual(result["title"], "Song")
        self.assertEqual(result["performer"], "Artist")

    def test_open_failure_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch(
                "youtube_alt._open_video", side_effect=RuntimeError("nope")
            ):
                self.assertIsNone(
                    download_video_audio(
                        "https://www.youtube.com/watch?v=abc123", Path(tmp)
                    )
                )


class TestDownloadAudioFiles(unittest.TestCase):
    def test_playlist_caps_at_20(self):
        urls = [f"https://www.youtube.com/watch?v=v{i}" for i in range(25)]
        fake_playlist = mock.Mock()
        fake_playlist.video_urls = urls
        attempted = []

        def fake_download(url, tmpdir):
            attempted.append(url)
            return {"path": Path(tmpdir) / "x.m4a", "title": "t", "performer": "p"}

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("youtube_alt.Playlist", return_value=fake_playlist):
                with mock.patch(
                    "youtube_alt.download_video_audio", side_effect=fake_download
                ):
                    results = download_audio_files(
                        "https://www.youtube.com/playlist?list=PL1", Path(tmp)
                    )
        self.assertEqual(len(results), 20)
        self.assertEqual(len(attempted), 20)

    def test_single_video(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch(
                "youtube_alt.download_video_audio",
                return_value={"path": Path(tmp) / "x.m4a", "title": "t", "performer": "p"},
            ):
                results = download_audio_files(
                    "https://www.youtube.com/watch?v=abc123", Path(tmp)
                )
        self.assertEqual(len(results), 1)


if __name__ == "__main__":
    unittest.main()
