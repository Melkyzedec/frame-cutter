"""Tranche 2 : version de yt-dlp, masquage des chemins, noms Windows, formats FFmpeg,
recherche des exécutables et IP littérales.

Exemples exclusivement fictifs (préfixe CANARY_TEST_, domaine example.test).
"""

import os
import re
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import video_import
from frame_engine import (
    FrameCutterError,
    _available_output_dir,
    _find_binary,
    extract_frames,
    probe_video,
    read_preview_frame,
    require_ffmpeg,
    sanitize_user_error,
)
from video_import import _download_platform, parse_video_link


PROJECT = Path(__file__).resolve().parents[1]
PINNED_YT_DLP = re.search(r"^yt-dlp==(\S+)", (PROJECT / "requirements-import.txt").read_text(encoding="utf-8"),
                          re.M).group(1)
REEL = "https://www.instagram.com/reel/abcdefghijk/"


def _ffmpeg_or_none():
    try:
        return require_ffmpeg()[0]
    except FrameCutterError:
        return None


class _Reached(Exception):
    """Raised by the fake Popen: the download would have started."""


def _fake_tools(root: Path, version: str | None, *, with_ejs: bool = True) -> Path:
    tools = root / "CANARY_TEST_tools"
    for package in ("yt_dlp", "yt_dlp_ejs") if with_ejs else ("yt_dlp",):
        (tools / package).mkdir(parents=True)
        (tools / package / "__init__.py").write_text("", encoding="utf-8")
    if version is not None:
        (tools / "yt_dlp" / "version.py").write_text(
            f"# Fichier fictif\nRELEASE_GIT_HEAD = 'CANARY_TEST'\n__version__ = '{version}'\n", encoding="utf-8")
    return tools


class YtDlpVersionTests(unittest.TestCase):
    """MAJ-7 : la version de yt-dlp utilisée est celle épinglée dans requirements-import.txt."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.folder = self.root / "CANARY_TEST_import"
        self.folder.mkdir()

    def download(self, link=REEL):
        return _download_platform(parse_video_link(link), self.folder, threading.Event(), time.monotonic() + 30)

    def test_pinned_constant_matches_the_requirements_file(self):
        self.assertEqual(video_import.YT_DLP_VERSION, PINNED_YT_DLP)

    def test_local_components_with_the_pinned_version_are_used(self):
        tools = _fake_tools(self.root, PINNED_YT_DLP)
        with patch("video_import.IMPORT_TOOLS", tools), \
                patch("video_import.require_ffmpeg", return_value=("ffmpeg", "ffprobe")), \
                patch("video_import.subprocess.Popen", side_effect=_Reached) as popen:
            with self.assertRaises(_Reached):
                self.download()
        popen.assert_called_once()
        self.assertEqual(popen.call_args.kwargs["env"]["PYTHONPATH"], str(tools))

    def test_other_or_unknown_local_versions_are_refused_before_running(self):
        for version in ("2025.01.01", f"{PINNED_YT_DLP}.dev0", "", None):
            with self.subTest(version=version):
                root = Path(tempfile.mkdtemp(dir=self.root))
                tools = _fake_tools(root, version)
                with patch("video_import.IMPORT_TOOLS", tools), \
                        patch("video_import.require_ffmpeg", return_value=("ffmpeg", "ffprobe")), \
                        patch("video_import.subprocess.Popen", side_effect=_Reached) as popen:
                    with self.assertRaisesRegex(FrameCutterError, rf"version attendue \({re.escape(PINNED_YT_DLP)}\)"):
                        self.download()
                popen.assert_not_called()

    def test_refusal_message_does_not_echo_arbitrary_text(self):
        tools = _fake_tools(self.root, r"C:\Users\CANARY_TEST_USER\evil")
        with patch("video_import.IMPORT_TOOLS", tools), \
                patch("video_import.subprocess.Popen", side_effect=_Reached):
            with self.assertRaises(FrameCutterError) as ctx:
                self.download()
        self.assertNotIn("CANARY_TEST_USER", str(ctx.exception))
        self.assertIn("inconnue", str(ctx.exception))

    def test_globally_installed_version_is_checked(self):
        empty_tools = self.root / "CANARY_TEST_sans_composants"
        for version, allowed in ((PINNED_YT_DLP, True), ("2024.12.31", False)):
            with self.subTest(version=version):
                package = Path(tempfile.mkdtemp(dir=self.root)) / "yt_dlp"
                package.mkdir()
                (package / "__init__.py").write_text("", encoding="utf-8")
                (package / "version.py").write_text(f"__version__ = '{version}'\n", encoding="utf-8")
                spec = SimpleNamespace(submodule_search_locations=[str(package)])
                with patch("video_import.IMPORT_TOOLS", empty_tools), \
                        patch("video_import.find_spec", return_value=spec), \
                        patch("video_import.require_ffmpeg", return_value=("ffmpeg", "ffprobe")), \
                        patch("video_import.subprocess.Popen", side_effect=_Reached) as popen:
                    if allowed:
                        with self.assertRaises(_Reached):
                            self.download()
                        popen.assert_called_once()
                    else:
                        with self.assertRaisesRegex(FrameCutterError, "version attendue"):
                            self.download()
                        popen.assert_not_called()

    def test_frozen_downloader_reports_its_version_before_use(self):
        tools = self.root / "CANARY_TEST_tools"
        tools.mkdir()
        (tools / "yt-dlp.exe").write_bytes(b"CANARY_TEST")
        for reported, allowed in ((f"{PINNED_YT_DLP}\n", True), ("2023.03.04\n", False)):
            with self.subTest(reported=reported.strip()):
                completed = subprocess.CompletedProcess([], 0, stdout=reported, stderr="")
                with patch.object(sys, "frozen", True, create=True), \
                        patch("video_import.IMPORT_TOOLS", tools), \
                        patch("video_import.subprocess.run", return_value=completed) as run, \
                        patch("video_import.require_ffmpeg", return_value=("ffmpeg", "ffprobe")), \
                        patch("video_import.subprocess.Popen", side_effect=_Reached) as popen:
                    if allowed:
                        with self.assertRaises(_Reached):
                            self.download()
                    else:
                        with self.assertRaisesRegex(FrameCutterError, "version attendue"):
                            self.download()
                        popen.assert_not_called()
                    self.assertEqual(run.call_args.args[0], [str(tools / "yt-dlp.exe"), "--version"])


class SanitizePathTests(unittest.TestCase):
    """MIN-1 : chemins avec espaces et chemins relatifs."""

    LEAKS = ("CANARY_TEST_USER", "CANARY_TEST_DIR", "secret", "clip", "vacances", "CANARY_TEST_FILE")

    def assert_clean(self, text):
        for fragment in self.LEAKS:
            self.assertNotIn(fragment, text)
        self.assertNotRegex(text, r"[A-Za-z]:[\\/]")
        self.assertNotIn("\\", text)

    def test_paths_with_spaces_are_fully_masked(self):
        for sample, kept in (
            (r"Échec sur C:\Users\CANARY_TEST_USER\My Videos\secret clip.mp4", "Échec sur"),
            (r"Échec « C:\CANARY_TEST_DIR\clip vacances.mp4 » refusé", "refusé"),
            (r"C:\CANARY_TEST_DIR\mes clips\secret clip.mp4: Invalid data found", "Invalid data found"),
            (r"Ouverture de D:\CANARY_TEST_DIR\clip vacances.mp4 : accès refusé", "accès refusé"),
            (r"\\serveur\partage\CANARY_TEST_DIR\clip vacances.mp4 introuvable", None),
            ("Lecture de /home/CANARY_TEST_USER/mes vidéos/secret clip.mp4 impossible", None),
            ("Lecture de /srv/CANARY_TEST_DIR/clip vacances.mp4: refusé", "refusé"),
        ):
            with self.subTest(sample=sample):
                cleaned = sanitize_user_error(sample)
                self.assert_clean(cleaned)
                self.assertIn("ce chemin", cleaned)
                if kept:
                    self.assertIn(kept, cleaned)

    def test_relative_and_single_segment_paths_are_masked(self):
        for sample in (
            r"Échec sur ..\CANARY_TEST_DIR\clip.mp4",
            r"Échec sur .\CANARY_TEST_DIR\clip vacances.mp4",
            "Échec sur ../CANARY_TEST_DIR/clip.mp4",
            "Échec sur ./CANARY_TEST_DIR/clip.mp4",
            "Échec sur ~/CANARY_TEST_USER/clip.mp4",
            r"Échec sur ~\CANARY_TEST_USER\clip.mp4",
            r"Échec sur CANARY_TEST_DIR\sub\clip.mp4",
            r"Échec sur %USERPROFILE%\CANARY_TEST_DIR\clip.mp4",
            "Échec sur /CANARY_TEST_FILE.mp4",
        ):
            with self.subTest(sample=sample):
                cleaned = sanitize_user_error(sample)
                self.assert_clean(cleaned)
                self.assertTrue(cleaned.startswith("Échec sur ce chemin"), cleaned)

    def test_application_messages_and_links_are_kept(self):
        for sample in (
            "Téléchargement gratuit : https://ffmpeg.org/download.html",
            "Ajoutez ffmpeg.exe et ffprobe.exe dans le dossier tools\\ffmpeg\\bin à côté de l’application.",
            "Choisissez entre 1 et 60 images/s, ou 1 image toutes les 5 s.",
            "Passage 1 sur 2. L’extraction a échoué.",
        ):
            with self.subTest(sample=sample):
                self.assertEqual(sanitize_user_error(sample), sample)

    def test_missing_ffmpeg_message_survives_sanitizing(self):
        with patch("frame_engine._find_binary", return_value=None):
            with self.assertRaises(FrameCutterError) as ctx:
                require_ffmpeg()
        cleaned = sanitize_user_error(str(ctx.exception))
        self.assertIn("tools\\ffmpeg\\bin", cleaned)
        self.assertIn("https://ffmpeg.org/download.html", cleaned)


class WindowsNameTests(unittest.TestCase):
    """MIN-3 : noms réservés Windows, espace ou point final, octet NUL."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.parent = Path(temp.name) / "sortie"
        self.parent.mkdir()

    def test_reserved_device_names_get_a_regular_suffixed_folder(self):
        stems = ("CON", "NUL", "nul", "COM1", "LPT1", "AUX", "PRN", "CON ", "clip ", "clip.", "CANARY_TEST\u00e9t\u00e9")
        for index, stem in enumerate(stems):
            with self.subTest(stem=stem):
                parent = self.parent / f"case_{index}"
                parent.mkdir()
                created = _available_output_dir(parent, stem)
                self.assertEqual(created.parent, parent.resolve())
                self.assertTrue(created.name.startswith(stem))
                self.assertTrue(created.name.endswith("_frames"))
                self.assertTrue(created.is_dir())
                self.assertEqual(list(created.iterdir()), [])

    def test_nul_bytes_and_separators_are_refused(self):
        for stem in ("CON\x00", "\x00", "CANARY_TEST\x00..", "CON/..", "NUL\\.."):
            with self.subTest(stem=repr(stem)):
                with self.assertRaisesRegex(FrameCutterError, "Nom de dossier de résultat invalide"):
                    _available_output_dir(self.parent, stem)
        self.assertEqual(list(self.parent.iterdir()), [])


@unittest.skipIf(_ffmpeg_or_none() is None, "FFmpeg est requis.")
class FormatWhitelistTests(unittest.TestCase):
    """MIN-4 : seuls les formats vidéo et image attendus sont ouverts par FFmpeg."""

    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        cls.ffmpeg = _ffmpeg_or_none()
        cls.private = cls.root / "CANARY_TEST_prive"
        cls.private.mkdir()
        cls.segment = cls.private / "CANARY_TEST_segment.ts"
        cls.video = cls.private / "CANARY_TEST_video.mp4"
        for target, codec in ((cls.segment, "mpeg2video"), (cls.video, "mpeg4")):
            subprocess.run([cls.ffmpeg, "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=160x90:rate=25",
                            "-t", "2", "-c:v", codec, str(target)], check=True, capture_output=True, timeout=15)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def traps(self):
        concat = self.root / "CANARY_TEST_concat.mp4"
        concat.write_text(f"ffconcat version 1.0\nfile CANARY_TEST_prive/{self.video.name}\n", encoding="ascii")
        hls = self.root / "CANARY_TEST_local.m3u8"
        hls.write_text("#EXTM3U\n#EXT-X-TARGETDURATION:2\n#EXTINF:2,\n"
                       f"CANARY_TEST_prive/{self.segment.name}\n#EXT-X-ENDLIST\n", encoding="ascii")
        return concat, hls

    def test_local_concat_and_hls_files_are_refused(self):
        for trap in self.traps():
            with self.subTest(trap=trap.name):
                destination = Path(tempfile.mkdtemp(dir=self.root))
                with self.assertRaisesRegex(FrameCutterError, "format de fichier non pris en charge"):
                    probe_video(trap)
                with self.assertRaisesRegex(FrameCutterError, "format de fichier non pris en charge"):
                    extract_frames(trap, destination, 2, "PNG", 2.0, lambda _: None, threading.Event())
                with self.assertRaisesRegex(FrameCutterError, "Aucune image disponible"):
                    read_preview_frame(trap, 0, 160, 90, threading.Event())
                self.assertEqual(list(destination.iterdir()), [])

    def test_supported_video_and_image_formats_still_open(self):
        for extension, codec in (("mp4", "mpeg4"), ("mov", "mpeg4"), ("m4v", "mpeg4"), ("mkv", "mpeg4"),
                                 ("webm", "libvpx"), ("avi", "mpeg4"), ("mpg", "mpeg2video"), ("mpeg", "mpeg2video"),
                                 ("ts", "mpeg2video"), ("wmv", "wmv2")):
            source = self.root / f"CANARY_TEST_format.{extension}"
            made = subprocess.run([self.ffmpeg, "-v", "error", "-n", "-f", "lavfi", "-i", "testsrc2=size=160x90:rate=25",
                                   "-t", "1", "-c:v", codec, str(source)], capture_output=True, timeout=15)
            if made.returncode != 0:
                continue  # codec absent de cette compilation de FFmpeg
            with self.subTest(extension=extension):
                self.assertGreater(probe_video(source).duration, 0)
                self.assertTrue(read_preview_frame(source, 0, 160, 90, threading.Event()).startswith(b"\x89PNG"))
        for image_format, extension in (("JPEG", "jpg"), ("PNG", "png")):
            with self.subTest(gallery=extension):
                output = extract_frames(self.video, Path(tempfile.mkdtemp(dir=self.root)), 1, image_format, 2.0,
                                        lambda _: None, threading.Event())
                frame = output / f"frame_000001.{extension}"
                self.assertTrue(read_preview_frame(frame, 0, 80, 45, threading.Event()).startswith(b"\x89PNG"))

    def test_every_ffmpeg_call_restricts_formats_before_the_input(self):
        commands = []

        def capture(command, **_options):
            commands.append(command)
            raise OSError("CANARY_TEST")

        destination = Path(tempfile.mkdtemp(dir=self.root))
        with patch("frame_engine.subprocess.Popen", side_effect=capture):
            for run in (lambda: probe_video(self.video),
                        lambda: read_preview_frame(self.video, 1.0, 160, 90, threading.Event()),
                        lambda: extract_frames(self.video, destination, 5, "JPEG", 2.0, lambda _: None,
                                               threading.Event())):
                with self.assertRaises(FrameCutterError):
                    run()
        self.assertEqual(len(commands), 3)
        for command in commands:
            with self.subTest(command=command[1:3]):
                self.assertIn("-format_whitelist", command)
                position = command.index("-format_whitelist")
                self.assertLess(position, command.index("-i"))
                allowed = set(command[position + 1].split(","))
                self.assertTrue({"mov", "matroska", "avi", "image2", "png_pipe"} <= allowed)
                self.assertTrue(allowed.isdisjoint({"concat", "hls", "tty", "lavfi", "ffmetadata"}))


def _make_executable(path: Path) -> Path:
    path.write_bytes(b"CANARY_TEST")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


class ExecutableLookupTests(unittest.TestCase):
    """MIN-6 : ffmpeg, ffprobe et node ne sont jamais pris dans le dossier courant."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name).resolve()
        self.cwd = root / "CANARY_TEST_courant"
        self.trusted = root / "CANARY_TEST_path"
        self.cwd.mkdir()
        self.trusted.mkdir()
        self.suffix = ".exe" if os.name == "nt" else ""
        previous = Path.cwd()
        os.chdir(self.cwd)
        self.addCleanup(os.chdir, previous)
        self.no_local_tools = patch("frame_engine.FFMPEG_DIR", root / "CANARY_TEST_absent")
        self.no_local_tools.start()
        self.addCleanup(self.no_local_tools.stop)

    def plant(self, folder, name):
        return _make_executable(folder / f"{name}{self.suffix}")

    def test_binaries_in_the_current_directory_are_ignored(self):
        for name in ("ffmpeg", "ffprobe"):
            self.plant(self.cwd, name)
        for path_value in (os.curdir, "", os.pathsep.join([os.curdir, ""]), str(self.trusted)):
            with self.subTest(PATH=path_value), patch.dict(os.environ, {"PATH": path_value}):
                self.assertIsNone(_find_binary("ffmpeg"))
                with self.assertRaisesRegex(FrameCutterError, "composant vidéo est absent"):
                    require_ffmpeg()

    def test_absolute_path_entries_are_used_and_returned_absolute(self):
        for name in ("ffmpeg", "ffprobe"):
            self.plant(self.cwd, name)
            self.plant(self.trusted, name)
        path_value = os.pathsep.join([os.curdir, "CANARY_TEST_relatif", str(self.trusted)])
        with patch.dict(os.environ, {"PATH": path_value}):
            ffmpeg, ffprobe = require_ffmpeg()
        self.assertEqual(Path(ffmpeg), self.trusted / f"ffmpeg{self.suffix}")
        self.assertEqual(Path(ffprobe), self.trusted / f"ffprobe{self.suffix}")

    def test_node_in_the_current_directory_is_ignored(self):
        self.plant(self.cwd, "node")
        tools = _fake_tools(self.cwd.parent, PINNED_YT_DLP)
        folder = self.cwd.parent / "CANARY_TEST_import"
        folder.mkdir()
        with patch.dict(os.environ, {"PATH": os.curdir}), \
                patch("video_import.IMPORT_TOOLS", tools), \
                patch("video_import.require_ffmpeg", return_value=("ffmpeg", "ffprobe")), \
                patch("video_import.subprocess.Popen", side_effect=_Reached) as popen:
            with self.assertRaisesRegex(FrameCutterError, "Node.js 22"):
                _download_platform(parse_video_link("https://youtu.be/abcdefghijk"), folder, threading.Event(),
                                   time.monotonic() + 30)
        popen.assert_not_called()


class LiteralAddressTests(unittest.TestCase):
    """MIN-9 : les IP littérales non publiques sont refusées dès la saisie du lien."""

    def test_non_public_literal_addresses_are_refused_early(self):
        for host in ("127.0.0.1", "127.1", "2130706433", "0x7f.0.0.1", "0177.0.0.1", "10.0.0.1",
                     "169.254.169.254", "192.168.1.10", "100.64.0.1", "0.0.0.0", "224.0.0.1",
                     "[::1]", "[::ffff:127.0.0.1]", "[fe80::1%25eth0]", "[fec0::1]", "[fc00::1]", "[ff02::1]"):
            url = f"https://{host}/CANARY_TEST.mp4"
            with self.subTest(url=url), patch("video_import.socket.getaddrinfo") as resolve:
                with self.assertRaisesRegex(FrameCutterError, "serveur public"):
                    parse_video_link(url)
                resolve.assert_not_called()

    def test_public_literals_and_names_are_still_accepted(self):
        for url in ("https://93.184.215.14/CANARY_TEST.mp4", "https://[2606:4700:4700::1111]/CANARY_TEST.mp4",
                    "https://example.test/CANARY_TEST.mp4", "https://dead.beef.example.test/CANARY_TEST.mp4"):
            with self.subTest(url=url):
                self.assertEqual(parse_video_link(url).url, url)


if __name__ == "__main__":
    unittest.main()
