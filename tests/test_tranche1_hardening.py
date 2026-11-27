"""Tranche 1 : garde-fous de sécurité et cas limites.

Exemples exclusivement fictifs (préfixe CANARY_TEST_, domaine example.test).
"""

import math
import os
import queue
import shutil
import socket
import subprocess
import tempfile
import threading
import tkinter as tk
import unittest
from contextlib import contextmanager
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

import video_import
from frame_engine import (
    FrameCutterError,
    _available_output_dir,
    _extract_portion,
    extract_frames,
    parse_position,
    read_preview_frame,
    require_ffmpeg,
)
from video_import import (
    IMPORT_MARKER,
    IMPORT_MARKER_CONTENT,
    clean_abandoned_imports,
    create_import_folder,
    download_video,
    parse_video_link,
    remove_import_folder,
    save_video_copy,
)


WITNESS_NAME = "CANARY_TEST_temoin.txt"
WITNESS_DATA = b"CANARY_TEST_contenu_a_preserver\n"


def _ffmpeg_or_none():
    try:
        return require_ffmpeg()[0]
    except FrameCutterError:
        return None


def _link_dir(test: unittest.TestCase, link: Path, target: Path) -> None:
    """Create a directory symlink, or a junction on Windows; skip the test when neither is possible."""
    try:
        os.symlink(target, link, target_is_directory=True)
        return
    except (OSError, NotImplementedError):
        pass
    if os.name == "nt":
        try:
            import _winapi

            _winapi.CreateJunction(str(target), str(link))
            return
        except (ImportError, OSError):
            pass
    test.skipTest("Impossible de créer un lien de dossier dans cet environnement.")


def _make_victim(parent: Path, *, with_marker: bool = True) -> Path:
    victim = parent / "CANARY_TEST_victime"
    victim.mkdir()
    (victim / WITNESS_NAME).write_bytes(WITNESS_DATA)
    if with_marker:
        (victim / IMPORT_MARKER).write_bytes(IMPORT_MARKER_CONTENT)
    return victim


class ImportCleanupLinkTests(unittest.TestCase):
    """CRIT-1 : la suppression récursive ne touche jamais un dossier témoin."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        base = Path(temp.name).resolve()
        self.base = base
        self.temp_root = base / "tmp"
        self.outside = base / "hors-temp"
        self.temp_root.mkdir()
        self.outside.mkdir()
        self._gettempdir = patch("video_import.gettempdir", return_value=str(self.temp_root))
        self._gettempdir.start()
        self.addCleanup(self._gettempdir.stop)
        self.addCleanup(self._close_active_imports)

    def _close_active_imports(self):
        for folder in list(video_import._active_imports):
            if folder.is_relative_to(self.base):
                video_import._active_imports.pop(folder).close()

    def assert_victim_intact(self, victim: Path):
        self.assertTrue(victim.is_dir(), "Le dossier témoin a été supprimé.")
        self.assertEqual((victim / WITNESS_NAME).read_bytes(), WITNESS_DATA)

    def test_folder_named_like_an_import_without_hex_id_is_kept(self):
        victim_parent = self.temp_root
        victim = victim_parent / "frame-cutter-v2-CANARY_TEST_victime"
        victim.mkdir()
        (victim / WITNESS_NAME).write_bytes(WITNESS_DATA)
        (victim / IMPORT_MARKER).write_bytes(IMPORT_MARKER_CONTENT)
        clean_abandoned_imports()
        self.assert_victim_intact(victim)

    def test_symlinked_import_folder_keeps_its_target(self):
        victim = _make_victim(self.outside)
        link = self.temp_root / ("frame-cutter-v2-" + "c" * 32)
        _link_dir(self, link, victim)
        # Le lien doit être écarté par le garde-fou lui-même, sans compter sur la protection
        # propre à rmtree (qui varie selon la version de Python et le type de lien Windows).
        with patch("video_import.shutil.rmtree", wraps=shutil.rmtree) as rmtree:
            clean_abandoned_imports()
        rmtree.assert_not_called()
        self.assert_victim_intact(victim)
        self.assertTrue((victim / IMPORT_MARKER).is_file())

    def test_marker_that_is_a_link_is_not_trusted(self):
        folder = self.temp_root / ("frame-cutter-v2-" + "d" * 32)
        folder.mkdir()
        (folder / WITNESS_NAME).write_bytes(WITNESS_DATA)
        real_marker = self.outside / "CANARY_TEST_marqueur"
        real_marker.write_bytes(IMPORT_MARKER_CONTENT)
        try:
            os.symlink(real_marker, folder / IMPORT_MARKER)
        except (OSError, NotImplementedError):
            self.skipTest("Impossible de créer un lien de fichier dans cet environnement.")
        clean_abandoned_imports()
        self.assert_victim_intact(folder)
        self.assertEqual(real_marker.read_bytes(), IMPORT_MARKER_CONTENT)

    def test_links_inside_an_abandoned_import_are_not_followed(self):
        victim = _make_victim(self.outside, with_marker=False)
        abandoned = self.temp_root / ("frame-cutter-v2-" + "e" * 32)
        abandoned.mkdir()
        (abandoned / IMPORT_MARKER).write_bytes(IMPORT_MARKER_CONTENT)
        _link_dir(self, abandoned / "CANARY_TEST_lien", victim)
        clean_abandoned_imports()
        self.assertFalse(abandoned.exists(), "Le dossier d’import abandonné aurait dû être nettoyé.")
        self.assert_victim_intact(victim)

    def test_active_import_replaced_by_a_link_keeps_the_target(self):
        victim = _make_victim(self.outside)
        active = create_import_folder()
        moved = self.temp_root / "CANARY_TEST_deplace"
        try:
            active.rename(moved)
        except OSError:
            self.skipTest("Le dossier d’import ouvert ne peut pas être déplacé sur ce système.")
        _link_dir(self, active, victim)
        with patch("video_import.shutil.rmtree", wraps=shutil.rmtree) as rmtree:
            remove_import_folder(active)
        rmtree.assert_not_called()
        self.assert_victim_intact(victim)

    def test_import_folder_outside_the_temporary_directory_is_kept(self):
        folder = create_import_folder()
        (folder / WITNESS_NAME).write_bytes(WITNESS_DATA)
        with patch("video_import.gettempdir", return_value=str(self.outside)):
            remove_import_folder(folder)
            clean_abandoned_imports()
        self.assert_victim_intact(folder)
        self.assertNotIn(folder, video_import._active_imports)


class _NoConnectionServer:
    """A local TCP listener that records whether anything tried to connect."""

    def __enter__(self):
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.bind(("127.0.0.1", 0))
        self.socket.listen(8)
        self.port = self.socket.getsockname()[1]
        return self

    def connections(self) -> int:
        self.socket.setblocking(False)
        count = 0
        while True:
            try:
                connection, _ = self.socket.accept()
            except (BlockingIOError, InterruptedError):
                return count
            connection.close()
            count += 1

    def __exit__(self, *_args):
        self.socket.close()
        return False


def _remote_manifest(path: Path, port: int) -> Path:
    path.write_text(
        "#EXTM3U\n#EXT-X-TARGETDURATION:2\n#EXTINF:2,\n"
        f"http://127.0.0.1:{port}/CANARY_TEST_segment.ts\n#EXT-X-ENDLIST\n",
        encoding="ascii",
    )
    return path


class PreviewWhitelistTests(unittest.TestCase):
    """CRIT-2 : l’aperçu et la galerie ne sortent jamais sur le réseau."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def assert_file_only_whitelist(self, command):
        self.assertIn("-protocol_whitelist", command)
        position = command.index("-protocol_whitelist")
        self.assertEqual(command[position + 1], "file")
        self.assertLess(position, command.index("-i"), "La liste blanche doit précéder l’entrée.")

    @unittest.skipIf(_ffmpeg_or_none() is None, "FFmpeg est requis.")
    def test_preview_of_a_remote_playlist_makes_no_outbound_connection(self):
        for seconds in (0, 1.0):
            with self.subTest(seconds=seconds), _NoConnectionServer() as server:
                manifest = _remote_manifest(self.root / f"CANARY_TEST_lien_{seconds:g}.m3u8", server.port)
                with self.assertRaisesRegex(FrameCutterError, "Aucune image disponible"):
                    read_preview_frame(manifest, seconds, 160, 90, threading.Event())
                self.assertEqual(server.connections(), 0, "L’aperçu a ouvert une connexion réseau.")

    def test_preview_command_restricts_protocols_to_local_files(self):
        source = self.root / "CANARY_TEST_video.mp4"
        source.write_bytes(b"CANARY_TEST")
        for seconds in (0, 2.5):
            with self.subTest(seconds=seconds), \
                    patch("frame_engine.require_ffmpeg", return_value=("ffmpeg", "ffprobe")), \
                    patch("frame_engine.subprocess.Popen", side_effect=OSError("CANARY_TEST")) as popen:
                with self.assertRaisesRegex(FrameCutterError, "Impossible de lancer l’aperçu"):
                    read_preview_frame(source, seconds, 160, 90, threading.Event())
                self.assert_file_only_whitelist(popen.call_args.args[0])

    def test_gallery_decoder_restricts_protocols_to_local_files(self):
        from export_gallery import ExportGallery

        frame = _remote_manifest(self.root / "frame_000001.png", 9)
        results = queue.Queue()
        with patch("frame_engine.require_ffmpeg", return_value=("ffmpeg", "ffprobe")), \
                patch("frame_engine.subprocess.Popen", side_effect=OSError("CANARY_TEST")) as popen:
            ExportGallery._decode(results, 1, frame, threading.Event(), (160, 90))
        self.assertEqual(results.get_nowait(), (1, "Impossible d’afficher cette image."))
        self.assert_file_only_whitelist(popen.call_args.args[0])

    @unittest.skipIf(_ffmpeg_or_none() is None, "FFmpeg est requis.")
    def test_gallery_decoder_makes_no_outbound_connection(self):
        from export_gallery import ExportGallery

        with _NoConnectionServer() as server:
            frame = _remote_manifest(self.root / "frame_000001.png", server.port)
            results = queue.Queue()
            ExportGallery._decode(results, 7, frame, threading.Event(), (160, 90))
            self.assertEqual(results.get_nowait(), (7, "Impossible d’afficher cette image."))
            self.assertEqual(server.connections(), 0, "La galerie a ouvert une connexion réseau.")


class _DownloadResponse(BytesIO):
    status = 200

    def __init__(self, content, headers=None):
        super().__init__(content)
        self.headers = headers or {}


class InputGuardTests(unittest.TestCase):
    """MAJ-4 : chaque garde-fou d’entrée a son test, avec le message attendu."""

    def test_backslashes_in_links_are_rejected(self):
        for url in (
            "https://example.test\\CANARY_TEST_video.mp4",
            "https://www.youtube.com\\@example.test/watch?v=abcdefghijk",
            "https://example.test/CANARY_TEST\\..\\video.mp4",
        ):
            with self.subTest(url=url):
                with self.assertRaisesRegex(FrameCutterError, "lien HTTPS valide"):
                    parse_video_link(url)

    def test_instagram_links_only_use_the_standard_port(self):
        for url in ("https://www.instagram.com:8443/reel/abcdefghijk/",
                    "https://instagram.com:80/reel/abcdefghijk/"):
            with self.subTest(url=url):
                with self.assertRaisesRegex(FrameCutterError, "Reel Instagram public"):
                    parse_video_link(url)
        for url in ("https://www.instagram.com:443/reel/abcdefghijk/", "https://instagram.com/reel/abcdefghijk"):
            with self.subTest(url=url):
                self.assertEqual(parse_video_link(url).url, "https://www.instagram.com/reel/abcdefghijk/")

    def test_youtube_bounds_must_be_ordered_and_finite(self):
        for bounds in ((8, 5), (5, 5), (-1, 5), (0, math.inf), (math.nan, 5), (0, math.nan)):
            with self.subTest(bounds=bounds):
                with self.assertRaisesRegex(FrameCutterError, "La fin doit être après le début"):
                    parse_video_link("https://youtu.be/abcdefghijk", bounds)
        self.assertEqual(parse_video_link("https://youtu.be/abcdefghijk", (0, 5)).portion, (0, 5))

    def test_parse_position_accepts_the_documented_formats(self):
        for text, expected in (("12,5", 12.5), ("12.5", 12.5), ("01:12,5", 72.5), ("1:01:12,5", 3672.5),
                               ("60:00", 3600), ("0", 0), (" 7 ", 7), ("0:00:00.000001", 0.000001)):
            with self.subTest(text=text):
                self.assertAlmostEqual(parse_position(text), expected)

    def test_parse_position_rejects_invalid_values(self):
        for text in ("", "1:75", "1:60", "1:00:75", "1:60:00", "1e3", "-1", "+1", "1:2:3:4", "1::2",
                     "0.0000001", "\u0663", "\uff11\uff12", "12,5 s", "inf", "nan", "9" * 400):
            with self.subTest(text=text[:20]):
                with self.assertRaises(ValueError):
                    parse_position(text)

    def test_truncated_download_is_rejected(self):
        header = b"\x00\x00\x00\x18ftypisom"
        response = _DownloadResponse(header + b"x" * 20, {"Content-Length": "100"})
        with tempfile.TemporaryDirectory() as temp, \
                patch("video_import.MIN_FREE_BYTES", 0), \
                patch("video_import.build_opener") as opener:
            opener.return_value.open.return_value = response
            with self.assertRaisesRegex(FrameCutterError, "Le téléchargement est incomplet"):
                download_video(parse_video_link("https://example.test/CANARY_TEST.mp4"), Path(temp), threading.Event())

    def test_saved_copy_refuses_the_temporary_import_folder(self):
        with tempfile.TemporaryDirectory() as temp, patch("video_import.MIN_FREE_BYTES", 0):
            staging = Path(temp, "CANARY_TEST_import")
            nested = staging / "sous-dossier"
            nested.mkdir(parents=True)
            source = staging / "video-importee.mp4"
            source.write_bytes(b"CANARY_TEST_video")
            for destination in (staging, nested):
                with self.subTest(destination=destination.name):
                    with self.assertRaisesRegex(FrameCutterError, "en dehors de l’import temporaire"):
                        save_video_copy(source, destination, threading.Event())
            self.assertEqual(sorted(p.name for p in staging.iterdir()), ["sous-dossier", "video-importee.mp4"])
            self.assertEqual(list(nested.iterdir()), [])

    def test_non_ascii_direct_links_are_percent_encoded(self):
        link = parse_video_link("https://example.test/CANARY_TEST_vidéo.mp4?titre=été#fragment")
        self.assertEqual(link.url, "https://example.test/CANARY_TEST_vid%C3%A9o.mp4?titre=%C3%A9t%C3%A9")
        self.assertTrue(link.url.isascii())
        already_encoded = "https://example.test/CANARY_TEST%20video.mp4?a=1&b=%C3%A9"
        self.assertEqual(parse_video_link(already_encoded).url, already_encoded)

    def test_unencodable_address_gives_a_clear_error(self):
        with tempfile.TemporaryDirectory() as temp, patch("video_import.build_opener") as opener:
            opener.return_value.open.side_effect = UnicodeEncodeError("ascii", "é", 0, 1, "CANARY_TEST")
            with self.assertRaisesRegex(FrameCutterError, "caractères non pris en charge"):
                download_video(parse_video_link("https://example.test/CANARY_TEST.mp4"), Path(temp), threading.Event())


@contextmanager
def _fake_ffmpeg(lines, return_code=0):
    class Process:
        stdout = iter(lines)
        stderr = iter(())

        def wait(self):
            return return_code

    yield Process()


class EngineEdgeTests(unittest.TestCase):
    """MAJ-3, MAJ-6 et MIN-2 : cas limites du moteur d’extraction."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def run_portion(self, lines, output_dir):
        with patch("frame_engine._video_process", side_effect=lambda *_a, **_k: _fake_ffmpeg(lines)):
            return _extract_portion("ffmpeg", self.root / "CANARY_TEST.mp4", output_dir, 5, "jpg", 2.0,
                                    0.0, 2.0, 1, lambda _seconds: None, threading.Event())

    def test_unavailable_frame_counter_is_not_an_unexpected_error(self):
        output_dir = self.root / "CANARY_TEST_frames"
        output_dir.mkdir()
        with self.assertRaisesRegex(FrameCutterError, "Aucune image exportée"):
            self.run_portion(["frame=N/A\n", "out_time=N/A\n", "progress=continue\n",
                              "frame=N/A\n", "progress=end\n"], output_dir)

    def test_unavailable_frame_counter_keeps_the_last_known_count(self):
        output_dir = self.root / "CANARY_TEST_frames"
        output_dir.mkdir()
        for number in (1, 2):
            (output_dir / f"frame_{number:06d}.jpg").write_bytes(b"\xff\xd8\xff")
        next_frame = self.run_portion(["frame=2\n", "out_time=00:00:01.000000\n", "progress=continue\n",
                                       "frame=N/A\n", "progress=end\n"], output_dir)
        self.assertEqual(next_frame, 3)

    def test_names_with_consecutive_dots_are_allowed(self):
        parent = self.root / "sortie"
        parent.mkdir()
        created = _available_output_dir(parent, "CANARY_TEST_mon..clip")
        self.assertEqual(created, parent.resolve() / "CANARY_TEST_mon..clip_frames")
        self.assertTrue(created.is_dir())
        for stem in ("..", "../CANARY_TEST", "CANARY_TEST/..", "..\\CANARY_TEST"):
            with self.subTest(stem=stem):
                with self.assertRaisesRegex(FrameCutterError, "Nom de dossier de résultat invalide"):
                    _available_output_dir(parent, stem)

    @unittest.skipIf(_ffmpeg_or_none() is None, "FFmpeg est requis.")
    def test_real_extraction_with_dots_and_empty_portion(self):
        source = self.root / "CANARY_TEST_mon..clip.mp4"
        subprocess.run(
            [_ffmpeg_or_none(), "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=160x90:rate=30",
             "-t", "2", "-c:v", "mpeg4", str(source)],
            check=True, capture_output=True, timeout=15,
        )
        output = extract_frames(source, self.root, 5, "JPEG", 2.0, lambda _: None, threading.Event())
        self.assertEqual(output.name, "CANARY_TEST_mon..clip_frames")
        self.assertEqual(len(list(output.glob("frame_*.jpg"))), 10)

        # Un passage valide placé entre deux images source ne produit aucune image.
        before = sorted(p.name for p in self.root.iterdir())
        with self.assertRaisesRegex(FrameCutterError, "Aucune image exportée"):
            extract_frames(source, self.root, 5, "JPEG", 2.0, lambda _: None, threading.Event(),
                           portions=((1.99, 2.0),))
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), before,
                         "Le dossier vide d’un export sans image doit être retiré.")


class UnexpectedErrorLoggingTests(unittest.TestCase):
    """MAJ-6 : les erreurs inattendues sont journalisées, sans chemin personnel."""

    CANARY_WIN = r"C:\Users\CANARY_TEST_USER\Videos\secret_clip.mp4"
    CANARY_UNIX = "/home/CANARY_TEST_USER/secret_clip.mp4"

    def setUp(self):
        from app import FrameCutterApp

        self.root = tk.Tk()
        self.root.withdraw()
        self.app = FrameCutterApp(self.root)
        self.addCleanup(self._destroy)

    def _destroy(self):
        if self.app._poll_job is not None:
            self.root.after_cancel(self.app._poll_job)
            self.app._poll_job = None
        self.app.preview.clear()
        self.app.gallery.destroy()
        self.root.destroy()

    def assert_logged_without_paths(self, logs, context):
        output = "\n".join(logs.output)
        self.assertIn(context, output)
        self.assertIn("RuntimeError", output)
        self.assertNotIn("CANARY_TEST_USER", output)
        self.assertNotIn("secret_clip", output)
        self.assertNotRegex(output, r"[A-Za-z]:[\\/]")
        self.assertNotIn("/home/", output)
        self.assertNotIn("/Users/", output)
        self.assertNotRegex(output, r"(?m)^\s*File \"")
        for record in logs.records:
            self.assertIsNone(record.exc_info, "La trace brute contient des chemins absolus.")

    def crash(self):
        return RuntimeError(f"Crash at {self.CANARY_WIN} and {self.CANARY_UNIX}")

    def test_each_worker_logs_unexpected_errors(self):
        app = self.app
        cases = (
            ("analyse", "app.probe_video",
             lambda op: app._probe_worker(op, Path("CANARY_TEST.mp4"))),
            ("extraction", "app.extract_frames",
             lambda op: app._extract_worker(op, Path("CANARY_TEST.mp4"), Path("."), 5.0, "JPEG", 2.0,
                                            ((0.0, 2.0),), None)),
            ("import", "app.download_video",
             lambda op: app._import_worker(op, parse_video_link("https://example.test/CANARY_TEST.mp4"))),
            ("enregistrement", "app.download_video",
             lambda op: app._save_video_worker(op, parse_video_link("https://example.test/CANARY_TEST.mp4"),
                                               Path("."))),
        )
        for context, target, run in cases:
            with self.subTest(context=context):
                operation = app._begin_operation()
                with patch(target, side_effect=self.crash()), \
                        self.assertLogs("frame_cutter", level="ERROR") as logs:
                    run(operation)
                self.assert_logged_without_paths(logs, context)
                _, kind, payload = app.events.get_nowait()
                self.assertEqual(kind, "error")
                self.assertRegex(str(payload), "Erreur inattendue")
                self.assertNotIn("CANARY_TEST_USER", str(payload))


if __name__ == "__main__":
    unittest.main()
