import socket
import tempfile
import time
import threading
import unittest
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from frame_engine import FrameCutterError, OperationCancelled
from video_import import (
    IMPORT_MARKER,
    IMPORT_MARKER_CONTENT,
    _check_platform_budget,
    _connect_public,
    clean_abandoned_imports,
    create_import_folder,
    download_video,
    parse_video_link,
    remove_import_folder,
    save_video_copy,
    _download_platform,
)


class DownloadResponse(BytesIO):
    status = 200

    def __init__(self, content, headers=None):
        super().__init__(content)
        self.headers = headers or {}


class VideoImportSecurityTests(unittest.TestCase):
    def download(self, destination, response):
        with patch("video_import.build_opener") as opener:
            opener.return_value.open.return_value = response
            return download_video(parse_video_link("https://example.com/video.mp4"), destination, threading.Event())

    def test_known_size_is_checked_before_reading(self):
        with tempfile.TemporaryDirectory() as temp, patch("video_import.MAX_IMPORT_BYTES", 32):
            response = DownloadResponse(b"", {"Content-Length": "9000"})
            with patch.object(response, "read", wraps=response.read) as read:
                with self.assertRaisesRegex(FrameCutterError, "limite de téléchargement"):
                    self.download(Path(temp), response)
                read.assert_not_called()
            self.assertEqual(list(Path(temp).iterdir()), [])

    def test_missing_content_length_cannot_exceed_limit(self):
        with tempfile.TemporaryDirectory() as temp, patch("video_import.MAX_IMPORT_BYTES", 32):
            header = b"\x00\x00\x00\x18ftypisom"
            with self.assertRaisesRegex(FrameCutterError, "limite de téléchargement"):
                self.download(Path(temp), DownloadResponse(header + b"x" * 64))
            [partial] = Path(temp).iterdir()
            self.assertEqual(partial.stat().st_size, len(header))

    def test_low_disk_space_blocks_download_and_platform_growth_is_counted(self):
        with tempfile.TemporaryDirectory() as temp:
            with patch("video_import.shutil.disk_usage", return_value=SimpleNamespace(free=1)):
                with self.assertRaisesRegex(FrameCutterError, "espace libre"):
                    self.download(Path(temp), DownloadResponse(b""))
            Path(temp, "intermediate.part").write_bytes(b"x" * 10)
            Path(temp, "video.mp4").write_bytes(b"x" * 10)
            with patch("video_import.MAX_IMPORT_BYTES", 15):
                with self.assertRaisesRegex(FrameCutterError, "limite de téléchargement"):
                    _check_platform_budget(Path(temp))

    def test_routes_to_checked_public_address_without_using_private_results(self):
        private = (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("192.168.0.2", 443))
        public = (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.215.14", 443))
        with patch("video_import.socket.getaddrinfo", return_value=[private, public]) as resolve, \
                patch("video_import.socket.socket", return_value=Mock()) as start_socket:
            connection = _connect_public(("example.com", 443), 1)
            resolve.assert_called_once()
            start_socket.assert_called_once()
            connection.connect.assert_called_once_with(public[-1])
        with patch("video_import.socket.getaddrinfo", return_value=[private]), \
                patch("video_import.socket.socket") as start_socket:
            with self.assertRaisesRegex(FrameCutterError, "serveur public"):
                _connect_public(("example.com", 443), 1)
            start_socket.assert_not_called()

    def test_cleanup_leaves_active_and_unrelated_folders(self):
        with tempfile.TemporaryDirectory() as temp, patch("video_import.gettempdir", return_value=temp):
            root = Path(temp)
            active = create_import_folder()
            unrelated = root / ("frame-cutter-v2-" + "a" * 32)
            unrelated.mkdir()
            abandoned = root / ("frame-cutter-v2-" + "b" * 32)
            abandoned.mkdir()
            (abandoned / IMPORT_MARKER).write_bytes(IMPORT_MARKER_CONTENT)
            (abandoned / "video.mp4").write_bytes(b"fixture")
            try:
                clean_abandoned_imports()
                self.assertTrue(active.exists())
                self.assertTrue(unrelated.exists())
                self.assertFalse(abandoned.exists())
            finally:
                remove_import_folder(active)


    def test_saved_copy_uses_unique_names_and_cleans_staging_after_cancel(self):
        with tempfile.TemporaryDirectory() as temp, patch("video_import.MIN_FREE_BYTES", 0):
            root = Path(temp)
            staging = root / "staging"
            destination = root / "chosen"
            staging.mkdir()
            destination.mkdir()
            source = staging / "video.mp4"
            source.write_bytes(b"fixture-video" + b"x" * 600_000)

            first = save_video_copy(source, destination, threading.Event())
            second = save_video_copy(source, destination, threading.Event())
            self.assertEqual((first.name, second.name), ("video.mp4", "video (2).mp4"))
            self.assertEqual(first.read_bytes(), source.read_bytes())
            self.assertEqual(second.read_bytes(), source.read_bytes())

            class CancelAfterTwoBlocks:
                def __init__(self):
                    self.calls = 0

                def is_set(self):
                    self.calls += 1
                    return self.calls >= 4

            with self.assertRaises(OperationCancelled):
                save_video_copy(source, destination, CancelAfterTwoBlocks())
            self.assertEqual({path.name for path in destination.iterdir()},
                             {"video.mp4", "video (2).mp4"})

    def test_platform_recording_requests_audio_and_keeps_download_guards(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            link = parse_video_link("https://youtu.be/abcdefghijk")
            commands = []

            class Completed:
                returncode = 0

                def __enter__(self):
                    return self

                def __exit__(self, *_args):
                    return False

                def poll(self):
                    return 0

                def communicate(self, timeout=None):
                    return "", None

            def start(command, cwd, **_options):
                commands.extend(command)
                Path(cwd, "youtube-abcdefghijk.mkv").write_bytes(b"video with audio")
                return Completed()

            with patch("video_import.find_spec", return_value=object()), patch("video_import.require_ffmpeg", return_value=("ffmpeg.exe", "ffprobe.exe")), patch("video_import.shutil.which", return_value="node.exe"), patch("video_import.subprocess.Popen", side_effect=start):
                downloaded = _download_platform(
                    link, folder, threading.Event(), time.monotonic() + 30, preserve_audio=True,
                )

            self.assertEqual(downloaded.name, "youtube-abcdefghijk.mkv")
            self.assertIn("bv+ba/b/bv", commands)
            self.assertIn("--merge-output-format", commands)
            self.assertIn("--remux-video", commands)
            for guard in ("--ignore-config", "--no-plugin-dirs", "--no-playlist",
                          "--no-cookies", "--no-cookies-from-browser", "--use-extractors"):
                self.assertIn(guard, commands)
            self.assertEqual(commands[-2:], ["--", link.url])

if __name__ == "__main__":
    unittest.main()
