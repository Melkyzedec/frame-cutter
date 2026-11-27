"""Chantiers UX prioritaires (audit Pôle 3) : lanceur, prérequis, messages clairs, fin d’export.

Exemples exclusivement fictifs (préfixe CANARY_TEST_, domaine example.test).
Aucune de ces vérifications ne relâche une protection de sécurité : les cas de blocage
(listes de lecture, protocoles externes, version épinglée, exécutables du dossier courant)
restent couverts par les autres fichiers de tests.
"""

import ast
import errno
import io
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import tkinter as tk
import unittest
from contextlib import redirect_stderr
from email.message import Message
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from uuid import uuid4

import app as app_module
import video_import
from app import FrameCutterApp
from frame_engine import FrameCutterError, format_frame_rate, require_ffmpeg
from link_dialog import LinkDialog
from video_import import _download_platform, download_video, parse_video_link, platform_problems


PROJECT = Path(__file__).resolve().parents[1]
LAUNCHER = PROJECT / "Lancer Frame Cutter.bat"
PINNED_YT_DLP = re.search(r"^yt-dlp==(\S+)", (PROJECT / "requirements-import.txt").read_text(encoding="utf-8"),
                          re.M).group(1)
NETWORK_MESSAGE = "Connexion impossible ou interrompue. Vérifiez votre accès Internet puis réessayez."
CANARY_WIN = r"C:\Users\CANARY_TEST_USER\Videos\CANARY_TEST_clip.mp4"
JARGON = re.compile(r"ffprobe|\bPATH\b|HTTP \d|HTTP \{|whitelist|README|\bDLL\b", re.IGNORECASE)


def _ffmpeg_or_none():
    try:
        return require_ffmpeg()[0]
    except FrameCutterError:
        return None


def _fake_tools(root: Path, version: str | None = PINNED_YT_DLP, *, node: bool = True) -> Path:
    tools = root / "CANARY_TEST_tools"
    for package in ("yt_dlp", "yt_dlp_ejs"):
        (tools / package).mkdir(parents=True)
        (tools / package / "__init__.py").write_text("", encoding="utf-8")
    if version is not None:
        (tools / "yt_dlp" / "version.py").write_text(f"__version__ = '{version}'\n", encoding="utf-8")
    if node:
        (tools / "node.exe").write_bytes(b"CANARY_TEST")
    return tools


class _NoProcess(Exception):
    """Raised if a detection step tried to start a program."""


# --- Lot 1 : lanceur Windows, démarrage protégé, README simple -------------------------------------


class LauncherTests(unittest.TestCase):
    maxDiff = 400

    def setUp(self):
        self.assertTrue(LAUNCHER.is_file(), "Le lanceur « Lancer Frame Cutter.bat » doit exister à la racine.")
        self.raw = LAUNCHER.read_bytes()
        self.text = self.raw.decode("utf-8")
        self.lowered = self.text.lower()

    def test_launcher_is_plain_utf8_without_bom(self):
        self.assertFalse(self.raw.startswith(b"\xef\xbb\xbf"))
        self.assertIn("chcp 65001", self.lowered)

    def test_launcher_checks_python_version_and_tkinter_before_starting(self):
        for expected in ("py -3", "python", "(3, 11)", "import tkinter", "app.py"):
            with self.subTest(expected=expected):
                self.assertIn(expected, self.text)
        self.assertLess(self.text.index("import tkinter"), self.text.rindex("app.py"))

    def test_launcher_explains_each_failure_in_french_then_pauses(self):
        for message in ("Python 3.11 ou plus récent est introuvable", "Tkinter", "https://www.python.org/downloads/windows/",
                        "puis relancez"):
            with self.subTest(message=message):
                self.assertIn(message, self.text)
        self.assertGreaterEqual(self.lowered.count("pause"), 3, "Chaque échec doit attendre une touche.")

    def test_launcher_never_runs_a_program_from_the_current_folder(self):
        self.assertIn("NoDefaultCurrentDirectoryInExePath=1", self.text)
        # The interpreter is run by its absolute path, as reported by Python itself.
        self.assertIn("sys.executable", self.text)
        self.assertNotIn("%CD%", self.text.upper())

    def test_launcher_has_windows_line_endings_on_checkout(self):
        attributes = (PROJECT / ".gitattributes").read_text(encoding="utf-8")
        self.assertRegex(attributes, r"(?m)^\*\.bat\s+text\s+eol=crlf\s*$")


class StartupTests(unittest.TestCase):
    def run_main(self):
        shown = []
        stderr = io.StringIO()
        # No logging handler may outlive the test, and no real temporary folder is touched.
        with patch("app._show_startup_error", side_effect=shown.append), patch("app.logging.basicConfig"), \
                patch("app.clean_abandoned_imports"), redirect_stderr(stderr):
            code = app_module.main()
        return code, shown, stderr.getvalue()

    def test_unexpected_startup_error_shows_a_french_message_without_traceback(self):
        with patch("app.tk.Tk", side_effect=RuntimeError(f"Crash {CANARY_WIN}")):
            code, shown, stderr = self.run_main()
        self.assertEqual(code, 1)
        self.assertEqual(len(shown), 1)
        self.assertIn("Frame Cutter n’a pas pu démarrer", shown[0])
        self.assertIn("relancez", shown[0].lower())
        self.assertNotIn("Traceback", stderr)
        self.assertNotIn("CANARY_TEST_USER", stderr + shown[0])

    def test_window_construction_failure_is_reported_and_window_closed(self):
        roots = []
        real_tk = tk.Tk

        def make_root():
            root = real_tk()
            root.withdraw()
            roots.append(root)
            return root

        with patch("app.tk.Tk", side_effect=make_root), \
                patch("app.FrameCutterApp", side_effect=ValueError("CANARY_TEST")):
            code, shown, stderr = self.run_main()
        self.assertEqual(code, 1)
        self.assertEqual(len(shown), 1)
        self.assertNotIn("Traceback", stderr)
        with self.assertRaises(tk.TclError):
            roots[0].winfo_exists()

    def test_missing_tkinter_is_explained_before_any_window(self):
        problem = app_module.startup_problem(gui_error=ModuleNotFoundError("No module named 'tkinter'", name="tkinter"))
        self.assertIn("Tkinter", problem)
        self.assertIn("relancez", problem.lower())
        with patch("app._GUI_IMPORT_ERROR", ModuleNotFoundError("No module named '_tkinter'", name="_tkinter")), \
                patch("app.tk.Tk") as make_root:
            code, shown, _ = self.run_main()
        self.assertEqual(code, 1)
        make_root.assert_not_called()
        self.assertIn("Tkinter", shown[0])

    def test_damaged_installation_is_not_reported_as_missing_tkinter(self):
        problem = app_module.startup_problem(gui_error=ModuleNotFoundError("No module named 'link_dialog'",
                                                                           name="link_dialog"))
        self.assertNotIn("Tkinter", problem)
        self.assertIn("Réextrayez", problem)

    def test_old_python_is_refused_with_the_required_version(self):
        problem = app_module.startup_problem(version_info=(3, 10, 12))
        self.assertIn("Python 3.11 ou plus récent", problem)
        self.assertIsNone(app_module.startup_problem(version_info=(3, 11, 0)))

    def test_startup_message_falls_back_to_the_console(self):
        stderr = io.StringIO()
        with patch("app.tk", None), patch("app.os.name", "posix"), redirect_stderr(stderr):
            app_module._show_startup_error("Message CANARY_TEST")
        self.assertIn("Message CANARY_TEST", stderr.getvalue())


class ReadmeTests(unittest.TestCase):
    def setUp(self):
        self.text = (PROJECT / "README.md").read_text(encoding="utf-8")
        self.advanced = "## Import depuis un lien (avancé)"
        self.assertTrue(self.advanced in self.text, "Section avancée absente du README.")
        self.front = self.text[:self.text.index(self.advanced)]

    def test_readme_starts_with_three_simple_steps_and_the_launcher(self):
        start = "\n".join(self.text.splitlines()[:30])
        self.assertIn("Lancer Frame Cutter.bat", start)
        for step in ("1. ", "2. ", "3. "):
            self.assertIn(step, start)
        self.assertIn("Python 3.11", start)
        self.assertNotIn("3.14", self.text)

    def test_readme_keeps_technical_setup_in_the_advanced_section(self):
        for jargon in ("PowerShell", "PATH", "DLL", "pip ", "--target"):
            with self.subTest(jargon=jargon):
                self.assertNotIn(jargon, self.front)
        self.assertIn("pip install --target tools/link-import -r requirements-import.txt",
                      self.text[self.text.index(self.advanced):])

    def test_readme_uses_vous_everywhere(self):
        self.assertIsNone(re.search(r"\btu\b", self.text, re.IGNORECASE))


# --- Lot 2 : prérequis vérifiés à l’ouverture, plateformes, coupure réseau -------------------------


class WindowTestCase(unittest.TestCase):
    def new_window(self):
        root = tk.Tk()
        root.withdraw()
        app = FrameCutterApp(root)
        self.addCleanup(self.destroy_window, app)
        return root, app

    def destroy_window(self, app):
        try:
            closed = threading.Event()
            app.root.bind("<Destroy>", lambda event: closed.set() if event.widget is app.root else None, add="+")
            app._close()
            self.wait_for(app.root, closed.is_set)
        except tk.TclError:
            pass

    def wait_for(self, root, condition, timeout=6):
        deadline = time.monotonic() + timeout
        while not condition():
            if time.monotonic() >= deadline:
                self.fail("Le changement attendu dans la fenêtre n’est pas arrivé.")
            root.update()
            time.sleep(0.01)


class StartupPrerequisiteTests(WindowTestCase):
    def test_missing_video_component_is_announced_when_the_window_opens(self):
        missing = FrameCutterError("Le composant vidéo est absent.\n\nCopiez-le puis relancez.")
        with patch("app.require_ffmpeg", side_effect=missing):
            root, app = self.new_window()
        root.update_idletasks()
        banner = app.prerequisites_banner
        self.assertEqual(banner.winfo_manager(), "pack")
        self.assertIn("composant vidéo est absent", banner.cget("text"))
        self.assertNotIn("\n\n", banner.cget("text"))
        self.assertIn("composant vidéo", app.status.get())

    def test_no_banner_when_the_video_component_is_present(self):
        with patch("app.require_ffmpeg", return_value=("CANARY_TEST_ffmpeg", "CANARY_TEST_ffprobe")):
            _, app = self.new_window()
        self.assertEqual(app.prerequisites_banner.winfo_manager(), "")
        self.assertEqual(app.status.get(), "Choisissez une vidéo pour commencer.")


class PlatformDetectionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        for target in ("video_import.subprocess.Popen", "video_import.subprocess.run"):
            patcher = patch(target, side_effect=_NoProcess)
            patcher.start()
            self.addCleanup(patcher.stop)

    def detect(self, tools, node=None):
        with patch("video_import.IMPORT_TOOLS", tools), patch("video_import.find_spec", return_value=None), \
                patch("video_import.find_executable", return_value=node):
            return platform_problems()

    def test_both_platforms_are_usable_with_pinned_components_and_node(self):
        self.assertEqual(self.detect(_fake_tools(self.root)), {"youtube": None, "instagram": None})

    def test_missing_components_disable_both_platforms_with_a_clear_reason(self):
        problems = self.detect(self.root / "CANARY_TEST_absent")
        for platform in ("youtube", "instagram"):
            with self.subTest(platform=platform):
                self.assertIn("n’est pas installé", problems[platform])
                self.assertIn("Import depuis un lien (avancé)", problems[platform])
                self.assertIsNone(JARGON.search(problems[platform]))

    def test_wrong_version_is_not_offered(self):
        problems = self.detect(_fake_tools(self.root, "2000.01.01"))
        self.assertIn(f"version attendue ({PINNED_YT_DLP})", problems["youtube"])
        self.assertIn(f"version attendue ({PINNED_YT_DLP})", problems["instagram"])

    def test_youtube_needs_node_but_instagram_does_not(self):
        problems = self.detect(_fake_tools(self.root, node=False))
        self.assertIn("Node.js 22", problems["youtube"])
        self.assertIsNone(JARGON.search(problems["youtube"]))
        self.assertIsNone(problems["instagram"])


class LinkDialogTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.addCleanup(self.root.destroy)

    def open_dialog(self, youtube=None, instagram=None):
        with patch.object(LinkDialog, "wait_window"), patch.object(LinkDialog, "wait_visibility"), \
                patch.object(LinkDialog, "grab_set"):
            dialog = LinkDialog(self.root, title="CANARY_TEST", platforms={"youtube": youtube, "instagram": instagram})
        self.addCleanup(lambda: dialog.winfo_exists() and dialog.destroy())
        return dialog

    def type_url(self, dialog, text):
        dialog.url.delete(0, "end")
        dialog.url.insert(0, text)
        dialog.update_idletasks()

    def test_available_platforms_are_offered(self):
        dialog = self.open_dialog()
        self.assertIn("YouTube", dialog.source_label.cget("text"))
        self.assertIn("Instagram", dialog.source_label.cget("text"))
        self.assertEqual(dialog.platform_notice.winfo_manager(), "")

    def test_unavailable_platforms_are_not_offered_and_direct_links_still_work(self):
        reason = "Le composant d’import CANARY_TEST n’est pas installé."
        dialog = self.open_dialog(youtube=reason, instagram=reason)
        self.assertNotIn("YouTube", dialog.source_label.cget("text"))
        self.assertNotIn("Instagram", dialog.source_label.cget("text"))
        notice = dialog.platform_notice.cget("text")
        self.assertEqual(dialog.platform_notice.winfo_manager(), "pack")
        self.assertIn("liens directs", notice)
        self.assertIn("YouTube et Instagram", notice)
        self.assertIn(reason, notice)
        self.assertEqual(dialog.excerpt.winfo_manager(), "", "L’extrait YouTube est masqué si YouTube est indisponible.")

        self.type_url(dialog, "https://youtu.be/abcdefghijk")
        self.assertFalse(dialog.validate())
        self.assertIn(reason, dialog.notice.cget("text"))

        self.type_url(dialog, "https://example.test/CANARY_TEST.mp4")
        self.assertTrue(dialog.validate())
        self.assertIsNone(dialog.result.platform)

    def test_only_youtube_missing_keeps_instagram(self):
        dialog = self.open_dialog(youtube="L’import YouTube nécessite Node.js 22 ou plus récent.")
        self.assertNotIn("YouTube", dialog.source_label.cget("text"))
        self.assertIn("Instagram", dialog.source_label.cget("text"))
        self.assertIn("Node.js 22", dialog.platform_notice.cget("text"))

    def test_excerpt_fields_are_enabled_only_for_a_youtube_link(self):
        dialog = self.open_dialog()
        self.assertEqual(dialog.excerpt.winfo_manager(), "pack")
        self.assertTrue(dialog.start.instate(["disabled"]))
        self.assertTrue(dialog.end.instate(["disabled"]))
        self.type_url(dialog, "https://www.youtube.com/watch?v=abcdefghijk")
        self.assertTrue(dialog.start.instate(["!disabled"]))
        self.assertTrue(dialog.end.instate(["!disabled"]))
        for other in ("https://www.instagram.com/reel/abcdefghijk/", "https://example.test/CANARY_TEST.mp4"):
            with self.subTest(link=other):
                self.type_url(dialog, other)
                self.assertTrue(dialog.start.instate(["disabled"]))
                self.assertTrue(dialog.end.instate(["disabled"]))

    def test_bounds_typed_for_youtube_are_ignored_after_switching_to_another_link(self):
        dialog = self.open_dialog()
        self.type_url(dialog, "https://youtu.be/abcdefghijk")
        dialog.start.insert(0, "00:01")
        dialog.end.insert(0, "00:02")
        self.assertTrue(dialog.validate())
        self.assertEqual(dialog.result.portion, (1.0, 2.0))
        self.type_url(dialog, "https://example.test/CANARY_TEST.mp4")
        self.assertTrue(dialog.validate())
        self.assertIsNone(dialog.result.portion)

    def test_save_button_says_it_does_not_open_the_video(self):
        dialog = self.open_dialog()
        self.assertIn("sans ouvrir", dialog.save_button.cget("text"))
        self.assertIn("ouvre la vidéo dans Frame Cutter", dialog.action_hint.cget("text"))


class NetworkFailureTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.tools = _fake_tools(self.root)
        self.folder = self.root / "CANARY_TEST_import"
        self.folder.mkdir()

    def platform_failure(self, output, url="https://youtu.be/abcdefghijk"):
        class Process:
            returncode = 1

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def poll(self):
                return 1

            def communicate(self, timeout=None):
                return output, None

        with patch("video_import.IMPORT_TOOLS", self.tools), \
                patch("video_import.require_ffmpeg", return_value=("ffmpeg.exe", "ffprobe.exe")), \
                patch("video_import.subprocess.Popen", return_value=Process()):
            with self.assertRaises(FrameCutterError) as ctx:
                _download_platform(parse_video_link(url), self.folder, threading.Event(), time.monotonic() + 30)
        return str(ctx.exception)

    def test_platform_network_failures_are_reported_as_a_lost_connection(self):
        for output in (
            "ERROR: [youtube] abcdefghijk: Unable to download webpage: <urlopen error [Errno 11001] getaddrinfo failed>",
            "ERROR: Failed to resolve 'www.youtube.com' ([Errno -3] Temporary failure in name resolution)",
            "ERROR: Unable to download API page: Connection refused",
            "ERROR: [Errno 104] Connection reset by peer",
            "ERROR: Read timed out. (read timeout=10)",
            "ERROR: [WinError 10060] A connection attempt failed",
            "ERROR: Network is unreachable",
        ):
            for url in ("https://youtu.be/abcdefghijk", "https://www.instagram.com/reel/abcdefghijk/"):
                with self.subTest(output=output, url=url):
                    self.assertEqual(self.platform_failure(output, url), NETWORK_MESSAGE)

    def test_other_platform_failures_keep_their_specific_message(self):
        self.assertIn("publique", self.platform_failure("ERROR: [youtube] abcdefghijk: Private video"))
        self.assertIn("publique",
                      self.platform_failure("ERROR: Unable to download webpage: HTTP Error 404: Not Found"))
        self.assertIn("bloqué", self.platform_failure("ERROR: Unable to download webpage: HTTP Error 429"))

    def test_direct_link_network_failures_use_the_same_message(self):
        link = parse_video_link("https://example.test/CANARY_TEST.mp4")
        for error in (URLError(OSError(errno.ECONNREFUSED, "refused")), TimeoutError("CANARY_TEST")):
            with self.subTest(error=error), patch("video_import.build_opener") as build_opener:
                build_opener.return_value.open.side_effect = error
                with self.assertRaises(FrameCutterError) as ctx:
                    download_video(link, self.folder, threading.Event())
                self.assertEqual(str(ctx.exception), NETWORK_MESSAGE)


# --- Lot 3 : messages en français clair, cause + action ---------------------------------------------


def _user_message_strings(path: Path) -> list[tuple[str, int, str]]:
    """Collect the literal text of the messages a user can see in one module."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    targets = {"FrameCutterError", "OperationCancelled", "showerror", "showinfo", "showwarning", "set"}
    tables = {"_FFMPEG_ERROR_TABLE", "_FFMPEG_DEFAULT_MESSAGES"}
    found = []

    def collect(node, owner):
        for child in ast.walk(node):
            if isinstance(child, ast.Constant) and isinstance(child.value, str):
                found.append((owner, child.lineno, child.value))

    for function in [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.Module))]:
        owner = getattr(function, "name", "<module>")
        for node in ast.walk(function):
            if isinstance(node, ast.Call):
                name = getattr(node.func, "attr", getattr(node.func, "id", ""))
                if name in targets:
                    collect(node, owner)
            elif isinstance(node, ast.Assign) and any(getattr(t, "id", "") in tables for t in node.targets):
                # Only the messages (dict values): the keys and tokens match FFmpeg's own output.
                for table in [n for n in ast.walk(node.value) if isinstance(n, ast.Dict)]:
                    for value in table.values:
                        collect(value, owner)
    return found


class PlainLanguageTests(WindowTestCase):
    def test_user_messages_have_no_technical_jargon(self):
        for module in ("frame_engine.py", "video_import.py", "app.py", "link_dialog.py"):
            for owner, line, text in _user_message_strings(PROJECT / module):
                with self.subTest(module=module, line=line):
                    self.assertIsNone(JARGON.search(text), text)
                    if owner != "require_ffmpeg":
                        self.assertNotRegex(text, r"\bFFmpeg\b")

    def test_missing_video_component_message_gives_cause_and_action(self):
        with patch("frame_engine._find_binary", return_value=None):
            with self.assertRaises(FrameCutterError) as ctx:
                require_ffmpeg()
        message = str(ctx.exception)
        self.assertIn("composant vidéo", message)
        self.assertIn("puis relancez", message)
        self.assertIsNone(JARGON.search(message))

    def test_http_refusals_do_not_show_status_codes(self):
        link = parse_video_link("https://example.test/CANARY_TEST.mp4")
        with tempfile.TemporaryDirectory() as temp:
            for code in (401, 403, 404, 410, 429, 500, 503):
                error = HTTPError(link.url, code, "CANARY_TEST", Message(), None)
                with self.subTest(code=code), patch("video_import.build_opener") as build_opener:
                    build_opener.return_value.open.side_effect = error
                    with self.assertRaises(FrameCutterError) as ctx:
                        download_video(link, Path(temp), threading.Event())
                    message = str(ctx.exception)
                    self.assertNotIn(str(code), message)
                    self.assertNotIn("HTTP", message)
                    self.assertRegex(message, r"Vérifiez|Réessayez|ouvrez")

    def test_missing_destination_folder_suggests_another_folder(self):
        root, app = self.new_window()
        app.video_path = Path("CANARY_TEST.mp4")
        app.video_info = SimpleNamespace(duration=2.0)
        app.destination.set(str(PROJECT / ".test-work" / f"CANARY_TEST_absent_{uuid4().hex}"))
        with patch.object(app.preview, "portions", return_value=((0.0, 2.0),)), \
                patch("app.messagebox.showerror") as showerror:
            app._start_extraction()
        showerror.assert_called_once()
        self.assertIn("Choisissez un autre dossier", showerror.call_args.args[1])

    def test_unexpected_errors_suggest_what_to_do(self):
        _, app = self.new_window()
        cases = (
            ("app.probe_video", lambda op: app._probe_worker(op, Path("CANARY_TEST.mp4"))),
            ("app.extract_frames", lambda op: app._extract_worker(op, Path("CANARY_TEST.mp4"), Path("."), 5.0, "JPEG",
                                                                  2.0, ((0.0, 2.0),), None)),
            ("app.download_video", lambda op: app._import_worker(
                op, parse_video_link("https://example.test/CANARY_TEST.mp4"))),
        )
        for target, run in cases:
            with self.subTest(target=target):
                operation = app._begin_operation()
                with patch(target, side_effect=RuntimeError("CANARY_TEST")), self.assertLogs("frame_cutter", "ERROR"):
                    run(operation)
                _, kind, payload = app.events.get_nowait()
                self.assertEqual(kind, "error")
                self.assertIn("Erreur inattendue", str(payload))
                self.assertIn("Réessayez", str(payload))
        app.busy = False


# --- Lot 4 : fin d’export claire, fréquence lisible, aucun clic muet --------------------------------


class FrameRateTests(unittest.TestCase):
    def test_source_frame_rate_is_shown_as_a_readable_number(self):
        for raw, shown in (
            ("25/1", "25 images/s"), ("30000/1001", "29,97 images/s"), ("24000/1001", "23,98 images/s"),
            ("60", "60 images/s"), ("50/2", "25 images/s"), ("12.5", "12,5 images/s"),
        ):
            with self.subTest(raw=raw):
                self.assertEqual(format_frame_rate(raw), shown)

    def test_unknown_frame_rates_are_hidden(self):
        for raw in (None, "", "0/0", "N/A", "1/0", "-25/1", "abc", "1e999", "nan"):
            with self.subTest(raw=raw):
                self.assertIsNone(format_frame_rate(raw))


@unittest.skipIf(_ffmpeg_or_none() is None, "FFmpeg est requis.")
class ExportEndTests(WindowTestCase):
    @classmethod
    def setUpClass(cls):
        scratch_root = PROJECT / ".test-work"
        scratch_root.mkdir(exist_ok=True)
        cls.scratch = (scratch_root / f"ux-{uuid4().hex}").resolve()
        cls.scratch.mkdir()
        cls.addClassCleanup(shutil.rmtree, cls.scratch, ignore_errors=True)
        cls.source = cls.scratch / "CANARY_TEST.mp4"
        subprocess.run(
            [_ffmpeg_or_none(), "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=160x90:rate=30",
             "-t", "2", "-c:v", "mpeg4", str(cls.source)],
            check=True, capture_output=True, timeout=15,
        )

    def setUp(self):
        self.destination = self.scratch / self._testMethodName
        self.destination.mkdir()

    def choose_source(self, root, app):
        with patch("app.filedialog.askopenfilename", return_value=str(self.source)):
            app.video_button.invoke()
        self.wait_for(root, lambda: app.start_button.instate(["!disabled"]))
        app.destination.set(str(self.destination))

    def test_video_details_show_a_readable_frame_rate(self):
        root, app = self.new_window()
        self.choose_source(root, app)
        self.assertIn("30 images/s", app.video_details.get())
        self.assertNotIn("30/1", app.video_details.get())

    def test_end_of_export_shows_the_real_count_and_the_short_folder_name(self):
        root, app = self.new_window()
        self.choose_source(root, app)
        self.assertEqual(str(app.start_button.cget("style")), "Primary.TButton")
        with patch("app.messagebox.showerror") as showerror:
            app.start_button.invoke()
            self.wait_for(root, lambda: not app.busy)
            showerror.assert_not_called()
        self.assertEqual(app.result_dir.name, "CANARY_TEST_frames")
        self.assertEqual(app.status.get(), "Terminé : 10 images enregistrées dans CANARY_TEST_frames.")
        self.assertNotIn(str(self.destination), app.status.get())
        self.assertEqual(str(app.open_button.cget("style")), "Primary.TButton")
        self.assertNotEqual(str(app.start_button.cget("style")), "Primary.TButton")
        self.assertTrue(app.open_button.instate(["!disabled"]))

        # The next action puts the extraction back in front.
        with patch("app.filedialog.askopenfilename", return_value=str(self.source)):
            app.video_button.invoke()
        self.wait_for(root, lambda: not app.busy)
        self.assertEqual(str(app.start_button.cget("style")), "Primary.TButton")
        self.assertNotEqual(str(app.open_button.cget("style")), "Primary.TButton")

    def test_export_summary_counts_and_shortens(self):
        summary = app_module.export_summary
        self.assertEqual(summary(Path("CANARY_TEST_frames"), 1), "Terminé : 1 image enregistrée dans CANARY_TEST_frames.")
        self.assertEqual(summary(Path("CANARY_TEST_frames_2"), 12000),
                         "Terminé : 12\u202f000 images enregistrées dans CANARY_TEST_frames_2.")
        long_name = "CANARY_TEST_" + "x" * 80 + "_frames"
        shortened = summary(Path(long_name), 3)
        self.assertLess(len(shortened), 100)
        self.assertTrue(shortened.endswith("_frames."))

    def test_open_folder_explains_a_moved_or_deleted_result(self):
        _, app = self.new_window()
        app.result_dir = self.destination / "CANARY_TEST_frames"
        with patch("app.messagebox.showerror") as showerror, patch("app.subprocess.Popen") as popen:
            app._open_result()
        popen.assert_not_called()
        showerror.assert_called_once()
        message = showerror.call_args.args[1]
        self.assertIn("déplacé ou supprimé", message)
        self.assertIn("CANARY_TEST_frames", message)
        self.assertNotIn(str(self.destination), message)

    def test_saved_copy_status_names_the_file_without_its_full_path(self):
        root, app = self.new_window()
        saved = self.destination / "CANARY_TEST_dossier" / "CANARY_TEST_video.mkv"
        operation = app._begin_operation()
        app.events.put((operation, "saved", saved))
        self.wait_for(root, lambda: not app.busy)
        status = app.status.get()
        self.assertIn("CANARY_TEST_video.mkv", status)
        self.assertIn("CANARY_TEST_dossier", status)
        self.assertNotIn(str(self.destination), status)
        self.assertIn("n’est pas ouverte", status)


if __name__ == "__main__":
    unittest.main()
