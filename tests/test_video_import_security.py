import re
import socket
import sys
import tempfile
import time
import threading
import unittest
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.request import ProxyHandler, Request

from frame_engine import FrameCutterError, OperationCancelled
from video_import import (
    IMPORT_MARKER,
    IMPORT_MARKER_CONTENT,
    _HttpsRedirect,
    _PublicHttpsConnection,
    _PublicHttpsHandler,
    _check_platform_budget,
    _connect_public,
    _download_direct,
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

            # Composants d’import fictifs : version épinglée et node.exe fourni, rien n’est exécuté.
            tools_dir = tempfile.TemporaryDirectory()
            self.addCleanup(tools_dir.cleanup)
            tools = Path(tools_dir.name)
            pinned = re.search(r"^yt-dlp==(\S+)", (Path(__file__).resolve().parents[1] / "requirements-import.txt")
                               .read_text(encoding="utf-8"), re.M).group(1)
            for package in ("yt_dlp", "yt_dlp_ejs"):
                (tools / package).mkdir()
                (tools / package / "__init__.py").write_text("", encoding="utf-8")
            (tools / "yt_dlp" / "version.py").write_text(f"__version__ = '{pinned}'\n", encoding="utf-8")
            (tools / "node.exe").write_bytes(b"CANARY_TEST")
            with patch("video_import.IMPORT_TOOLS", tools), patch("video_import.require_ffmpeg", return_value=("ffmpeg.exe", "ffprobe.exe")), patch("video_import.subprocess.Popen", side_effect=start):
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
            # -P : le dossier d’import (cwd) n’est pas ajouté à sys.path du sous-processus.
            self.assertEqual(commands[:5], [sys.executable, "-B", "-P", "-m", "yt_dlp"])

    def test_https_redirect_rejects_downgrade_file_and_credentials(self):
        handler = _HttpsRedirect()
        request = Request("https://example.test/start.mp4")
        for blocked_url in (
            "http://example.test/video.mp4",
            "file:///C:/video.mp4",
            "https://CANARY_TEST_USER:CANARY_TEST_PASS@example.test/video.mp4",
        ):
            with self.subTest(blocked_url=blocked_url):
                with self.assertRaisesRegex(FrameCutterError, "lien HTTPS valide"):
                    handler.redirect_request(request, None, 302, "Found", {}, blocked_url)
        allowed = handler.redirect_request(request, None, 302, "Found", {}, "https://example.test/next.mp4")
        self.assertEqual(allowed.full_url, "https://example.test/next.mp4")

    def test_public_https_handler_and_direct_opener_chain(self):
        connection = _PublicHttpsConnection("example.test")
        self.assertIs(connection._create_connection, _connect_public)

        handler = _PublicHttpsHandler()
        request = Request("https://example.test/video.mp4")
        with patch.object(handler, "do_open", return_value="opened") as do_open:
            self.assertEqual(handler.https_open(request), "opened")
            self.assertIs(do_open.call_args.args[0], _PublicHttpsConnection)
            self.assertIs(do_open.call_args.args[1], request)

        captured_handlers = []
        response = DownloadResponse(b"\x00\x00\x00\x18ftypisom", {"Content-Length": "12"})

        def capture_opener(*handlers):
            captured_handlers.extend(handlers)
            return SimpleNamespace(open=lambda *_args, **_kwargs: response)

        with tempfile.TemporaryDirectory() as temp, \
                patch("video_import.MIN_FREE_BYTES", 0), \
                patch("video_import.build_opener", side_effect=capture_opener):
            _download_direct("https://example.test/video.mp4", Path(temp), threading.Event(), time.monotonic() + 30)

        self.assertEqual(len(captured_handlers), 3)
        self.assertIsInstance(captured_handlers[0], ProxyHandler)
        self.assertEqual(captured_handlers[0].proxies, {})
        self.assertIsInstance(captured_handlers[1], _PublicHttpsHandler)
        self.assertIsInstance(captured_handlers[2], _HttpsRedirect)

    def test_connect_public_blocks_ssrf_ipv4_and_ipv6_matrix(self):
        blocked_targets = (
            (socket.AF_INET, ("127.0.0.1", 443)),
            (socket.AF_INET, ("169.254.169.254", 443)),
            (socket.AF_INET, ("10.0.0.1", 443)),
            (socket.AF_INET, ("100.64.0.1", 443)),
            (socket.AF_INET6, ("::1", 443, 0, 0)),
            (socket.AF_INET6, ("::ffff:127.0.0.1", 443, 0, 0)),
            (socket.AF_INET6, ("fe80::1%1", 443, 0, 1)),
            (socket.AF_INET6, ("fc00::1", 443, 0, 0)),
            (socket.AF_INET6, ("2002:7f00:0001::", 443, 0, 0)),
            # Plages ajoutées (tranche 1) : site-local obsolète, multicast, réservé, NAT64, Teredo…
            (socket.AF_INET6, ("fec0::1", 443, 0, 0)),
            (socket.AF_INET6, ("ff02::1", 443, 0, 0)),
            (socket.AF_INET6, ("64:ff9b::7f00:1", 443, 0, 0)),
            (socket.AF_INET6, ("2001::1", 443, 0, 0)),
            (socket.AF_INET6, ("::127.0.0.1", 443, 0, 0)),
            (socket.AF_INET, ("0.0.0.0", 443)),
            (socket.AF_INET, ("198.18.0.1", 443)),
            (socket.AF_INET, ("224.0.0.1", 443)),
            (socket.AF_INET, ("239.255.255.250", 443)),
            (socket.AF_INET, ("240.0.0.1", 443)),
            (socket.AF_INET, ("255.255.255.255", 443)),
        )
        for family, sockaddr in blocked_targets:
            with self.subTest(addr=sockaddr[0]):
                entry = (family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", sockaddr)
                with patch("video_import.socket.getaddrinfo", return_value=[entry]), \
                        patch("video_import.socket.socket") as start_socket:
                    with self.assertRaisesRegex(FrameCutterError, "serveur public"):
                        _connect_public(("example.test", 443), 1)
                    start_socket.assert_not_called()

if __name__ == "__main__":
    unittest.main()
