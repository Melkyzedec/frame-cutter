import errno
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
import unittest
from http.client import IncompleteRead
from io import BytesIO
from pathlib import Path
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch
import frame_engine
from uuid import uuid4

import app as app_module
from app import FrameCutterApp
from frame_engine import (
    FrameCutterError,
    OperationCancelled,
    _available_output_dir,
    extract_frames,
    format_duration,
    probe_video,
    read_preview_frame,
    require_ffmpeg,
)
from video_import import parse_video_link


class FrameCutterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ffmpeg, cls.ffprobe = require_ffmpeg()
        project = Path(__file__).resolve().parents[1]
        scratch_root = project / ".test-work"
        scratch_root.mkdir(exist_ok=True)
        cls.scratch = (scratch_root / uuid4().hex).resolve()
        if cls.scratch.parent != scratch_root:
            raise RuntimeError("Le dossier d’essai doit rester dans le projet.")
        cls.scratch.mkdir()
        cls.addClassCleanup(shutil.rmtree, cls.scratch)
        cls.source = cls.scratch / "vidéo de test.mp4"
        subprocess.run(
            [cls.ffmpeg, "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=160x90:rate=30",
             "-t", "2", "-c:v", "mpeg4", str(cls.source)],
            check=True, capture_output=True, timeout=15,
        )

        cls.audio_source = cls.scratch / "vidéo avec son.mp4"
        subprocess.run(
            [cls.ffmpeg, "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=160x90:rate=30",
             "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100", "-t", "2",
             "-c:v", "mpeg4", "-c:a", "aac", "-shortest", str(cls.audio_source)],
            check=True, capture_output=True, timeout=15,
        )
    def setUp(self):
        self.destination = self.scratch / self._testMethodName
        self.destination.mkdir()

    def extract(self, fps=5, image_format="JPEG", cancel_event=None):
        return extract_frames(
            self.source, self.destination, fps, image_format, 2, lambda _: None,
            cancel_event if cancel_event is not None else threading.Event(),
        )

    def test_exports_both_formats_and_preserves_source_and_previous_output(self):
        original = hashlib.sha256(self.source.read_bytes()).digest()
        info = probe_video(self.source)
        self.assertEqual((info.duration, info.width, info.height), (2, 160, 90))

        jpeg = self.extract()
        first_export = {p.name: p.read_bytes() for p in jpeg.iterdir()}
        self.assertEqual(sorted(first_export), [f"frame_{n:06d}.jpg" for n in range(1, 11)])
        self.assertTrue(all(data.startswith(b"\xff\xd8\xff") for data in first_export.values()))

        png = self.extract(2.5, "PNG")
        self.assertEqual(png.name, f"{jpeg.name}_2")
        self.assertEqual(sorted(p.name for p in png.iterdir()), [f"frame_{n:06d}.png" for n in range(1, 6)])
        self.assertTrue(all(p.read_bytes().startswith(b"\x89PNG\r\n\x1a\n") for p in png.iterdir()))
        self.assertEqual({p.name: p.read_bytes() for p in jpeg.iterdir()}, first_export)
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).digest(), original)

    def test_interval_mode_exports_one_frame_per_interval_and_starts_each_portion(self):
        source = self.destination / "vidéo de vingt secondes.mp4"
        subprocess.run(
            [self.ffmpeg, "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=160x90:rate=30",
             "-t", "20", "-c:v", "mpeg4", str(source)],
            check=True, capture_output=True, timeout=20,
        )

        every_five_seconds = extract_frames(
            source, self.destination, 5, "JPEG", 20, lambda _: None, threading.Event(), interval_seconds=5,
        )
        self.assertEqual(len(list(every_five_seconds.glob("frame_*.jpg"))), 4)

        short_passage = extract_frames(
            source, self.destination, 5, "JPEG", 20, lambda _: None, threading.Event(),
            portions=((2, 4),), interval_seconds=5,
        )
        self.assertEqual(len(list(short_passage.glob("frame_*.jpg"))), 1)

        six_seconds = extract_frames(
            source, self.destination, 5, "JPEG", 20, lambda _: None, threading.Event(),
            portions=((0, 6),), interval_seconds=5,
        )
        self.assertEqual(len(list(six_seconds.glob("frame_*.jpg"))), 2)

        separate_cadences = extract_frames(
            source, self.destination, 5, "JPEG", 20, lambda _: None, threading.Event(),
            portions=((1, 3), (7, 9)), interval_seconds=5,
        )
        self.assertEqual([path.name for path in sorted(separate_cadences.glob("frame_*.jpg"))],
                         ["frame_000001.jpg", "frame_000002.jpg"])

    def test_rejects_invalid_input_without_creating_an_export(self):
        corrupt = self.destination / "illisible.mp4"
        corrupt.write_bytes(b"not a video")
        with self.assertRaisesRegex(FrameCutterError, "Impossible de lire cette vidéo"):
            probe_video(corrupt)
        with self.assertRaisesRegex(FrameCutterError, "n’existe plus"):
            probe_video(self.destination / "absente.mp4")
        for fps in (0, 0.999, 60.001, -5, float("nan"), float("inf")):
            with self.subTest(fps=fps), patch("frame_engine.subprocess.Popen") as popen:
                with self.assertRaisesRegex(FrameCutterError, "cadence doit être comprise entre 1 et 60"):
                    self.extract(fps=fps)
                popen.assert_not_called()
        with patch("frame_engine.subprocess.Popen") as popen:
            with self.assertRaisesRegex(FrameCutterError, "Format de sortie non pris en charge"):
                self.extract(image_format="GIF")
            popen.assert_not_called()
        with self.assertRaisesRegex(FrameCutterError, "dossier de destination"):
            extract_frames(self.source, corrupt, 5, "JPEG", 2, lambda _: None, threading.Event())
        self.assertEqual(list(self.destination.iterdir()), [corrupt])

    def test_exports_with_literal_percent_in_the_source_or_destination(self):
        percent_source = self.destination / "vidéo 100%.mp4"
        shutil.copyfile(self.source, percent_source)
        percent_destination = self.destination / "sortie 100%"
        percent_destination.mkdir()
        for source, destination, image_format, extension in (
            (percent_source, self.destination, "JPEG", "jpg"),
            (self.source, percent_destination, "PNG", "png"),
        ):
            with self.subTest(source=source.name, destination=destination.name):
                output = extract_frames(source, destination, 5, image_format, 2,
                                        lambda _: None, threading.Event())
                self.assertEqual(output, destination / f"{source.stem}_frames")
                self.assertEqual(sorted(p.name for p in output.iterdir()),
                                 [f"frame_{number:06d}.{extension}" for number in range(1, 11)])

    def test_extracts_from_variable_frame_rate_video(self):
        source = self.destination / "cadence variable.mp4"
        subprocess.run(
            [self.ffmpeg, "-v", "error", "-i", str(self.source),
             "-vf", r"select=if(lt(t\,1)\,not(mod(n\,2))\,not(mod(n-1\,3)))",
             "-fps_mode", "vfr", "-c:v", "mpeg4", str(source)],
            check=True, capture_output=True, timeout=15,
        )
        info = probe_video(source)
        self.assertNotEqual(info.frame_rate, "30/1")
        output = extract_frames(source, self.destination, 5, "JPEG", info.duration, lambda _: None, threading.Event())
        self.assertEqual(len(list(output.glob("*.jpg"))), 10)

    def test_extraction_explains_storage_errors(self):
        for error, message in (
            (OSError(errno.ENOSPC, "No space left on device"), "Espace disque insuffisant"),
            (PermissionError(errno.EACCES, "Permission denied"), "Choisissez un dossier accessible"),
        ):
            with self.subTest(error=error), patch.object(Path, "mkdir", side_effect=error):
                with self.assertRaisesRegex(FrameCutterError, message):
                    self.extract()

    def test_remote_playlist_is_rejected_by_both_video_tools(self):
        manifest = self.destination / "lien externe.m3u8"
        manifest.write_text(
            "#EXTM3U\n#EXT-X-TARGETDURATION:2\n#EXTINF:2,\n"
            "http://127.0.0.1:9/external.ts\n#EXT-X-ENDLIST\n", encoding="ascii",
        )
        # Blocage inchangé ; seul le libellé est désormais en français clair, sans « whitelist ».
        with self.assertRaisesRegex(FrameCutterError, "non prise? en charge") as ctx:
            probe_video(manifest)
        self.assertNotIn("whitelist", str(ctx.exception))
        with self.assertRaisesRegex(FrameCutterError, "non prise? en charge") as ctx:
            extract_frames(manifest, self.destination, 5, "JPEG", 2, lambda _: None, threading.Event())
        self.assertNotIn("whitelist", str(ctx.exception))
        with self.assertRaisesRegex(FrameCutterError, "Aucune image disponible"):
            read_preview_frame(manifest, 0, 160, 90, threading.Event())

    def test_cancellation_stops_a_silent_process(self):
        cancel = threading.Event()
        started = threading.Event()
        processes = []
        errors = []
        popen = subprocess.Popen

        def silent_process(_command, **options):
            process = popen([sys.executable, "-B", "-c", "import time; time.sleep(15)"], **options)
            processes.append(process)
            started.set()
            return process

        def run():
            try:
                self.extract(cancel_event=cancel)
            except Exception as exc:
                errors.append(exc)

        worker = threading.Thread(target=run, daemon=True)
        with patch("frame_engine.subprocess.Popen", side_effect=silent_process):
            try:
                worker.start()
                self.assertTrue(started.wait(3), "Le processus d’essai n’a pas démarré.")
                cancel.set()
                worker.join(3)
                self.assertFalse(worker.is_alive(), "L’arrêt dépend encore d’une ligne de progression.")
                self.assertIsNotNone(processes[0].poll())
                self.assertEqual(len(errors), 1)
                self.assertIsInstance(errors[0], FrameCutterError)
                self.assertIn("annulée", str(errors[0]))
            finally:
                for process in processes:
                    if process.poll() is None:
                        process.kill()
                    process.wait(timeout=3)
                worker.join(3)

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

    def wait_for(self, root, condition, timeout=4):
        deadline = time.monotonic() + timeout
        while not condition():
            if time.monotonic() >= deadline:
                self.fail("Le changement attendu dans la fenêtre n’est pas arrivé.")
            root.update()
            time.sleep(0.01)

    def choose_source(self, root, app, source=None):
        with patch("app.filedialog.askopenfilename", return_value=str(source or self.source)):
            app.video_button.invoke()
        self.wait_for(root, lambda: app.start_button.instate(["!disabled"]))
        app.destination.set(str(self.destination))

    def test_export_commands_stay_visible_with_a_long_name_in_small_windows(self):
        source = self.scratch / ("Extrait de démonstration avec un nom long " * 3 + ".mp4")
        shutil.copyfile(self.source, source)
        root, app = self.new_window()
        root.deiconify()
        self.choose_source(root, app, source)
        controls = (app.start_button, app.cancel_button, app.gallery_button, app.open_button, app.progress)
        for width, height in ((1040, 530), (390, 844)):
            with self.subTest(width=width, height=height):
                root.geometry(f"{width}x{height}")
                root.update()
                for control in controls:
                    self.assertTrue(control.winfo_viewable())
                    self.assertGreaterEqual(control.winfo_height(), control.winfo_reqheight())
                    self.assertLessEqual(control.winfo_rooty() + control.winfo_height(),
                                         root.winfo_rooty() + root.winfo_height())
                    self.assertLessEqual(control.winfo_rootx() + control.winfo_width(),
                                         root.winfo_rootx() + root.winfo_width())
                button_y = app.start_button.winfo_rooty()
                app.canvas.yview_moveto(1)
                root.update()
                self.assertEqual(app.start_button.winfo_rooty(), button_y)

    def test_window_previews_and_seeks_to_the_last_frame(self):
        source = self.destination / "rouge puis bleu.mp4"
        subprocess.run(
            [self.ffmpeg, "-v", "error", "-f", "lavfi", "-i", "color=red:s=160x90:d=1:r=30",
             "-f", "lavfi", "-i", "color=blue:s=160x90:d=1:r=30",
             "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0", "-c:v", "mpeg4", str(source)],
            check=True, capture_output=True, timeout=15,
        )
        root, app = self.new_window()
        self.assertTrue(app.preview.slider.instate(["disabled"]))
        root.deiconify()
        self.choose_source(root, app, source)

        def shown_pixel():
            photo = app.preview._photo
            return photo.get(0, 0) if photo is not None else None

        self.wait_for(root, lambda: shown_pixel() is not None)
        red, _, blue = shown_pixel()
        self.assertGreater(red, blue)

        slider = app.preview.slider
        start_x, y = slider.coords()
        end_x, _ = slider.coords(float(slider.cget("to")))
        slider.event_generate("<ButtonPress-1>", x=start_x, y=y)
        slider.event_generate("<B1-Motion>", x=end_x, y=y)
        slider.event_generate("<ButtonRelease-1>", x=end_x, y=y)
        self.wait_for(root, lambda: shown_pixel() is not None and shown_pixel()[2] > shown_pixel()[0])
        self.assertTrue(app.start_button.instate(["!disabled"]))

        self.choose_source(root, app)
        self.wait_for(root, lambda: app.preview._displayed == 0 and shown_pixel() is not None)

    def test_window_stops_an_active_preview_on_close(self):
        root, app = self.new_window()
        closed = threading.Event()
        root.bind("<Destroy>", lambda event: closed.set() if event.widget is root else None)
        popen = subprocess.Popen
        previews = []

        def slow_preview(command, **options):
            if "image2pipe" not in command:
                return popen(command, **options)
            process = popen([sys.executable, "-B", "-c", "import time; time.sleep(15)"], **options)
            previews.append(process)
            return process

        with patch("frame_engine.subprocess.Popen", side_effect=slow_preview):
            try:
                self.choose_source(root, app)
                self.wait_for(root, lambda: bool(previews))
                self.assertIsNone(previews[0].poll())
                root.tk.call(root.protocol("WM_DELETE_WINDOW"))
                self.wait_for(root, closed.is_set, timeout=3)
                self.assertIsNotNone(previews[0].poll())
            finally:
                for process in previews:
                    if process.poll() is None:
                        process.kill()
                    process.wait(timeout=3)

    def test_window_exports_only_the_frames_inside_the_selected_portion(self):
        source = self.destination / "rouge vert bleu.mp4"
        command = [self.ffmpeg, "-v", "error"]
        for color in ("red", "green", "blue"):
            command += ["-f", "lavfi", "-i", f"color={color}:s=160x90:d=1:r=30"]
        command += ["-filter_complex", "[0:v][1:v][2:v]concat=n=3:v=1:a=0", "-c:v", "mpeg4", str(source)]
        subprocess.run(command, check=True, capture_output=True, timeout=15)

        root, app = self.new_window()
        self.choose_source(root, app, source)
        self.assertEqual(app.preview.portion(), (0.0, 3.0))
        app.preview.slider.set(1)
        app.preview.use_start_button.invoke()
        app.preview.slider.set(2)
        app.preview.use_end_button.invoke()
        self.assertEqual(app.preview.portion(), (1.0, 2.0))
        self.assertIn("environ 5 images", app.estimate.get())

        for invalid_end in ("0,5", "1", "00:04", "illisible"):
            with self.subTest(invalid_end=invalid_end):
                app.preview.end.set(invalid_end)
                self.assertTrue(app.start_button.instate(["disabled"]))
        app.preview.end.set("00:02,0")
        self.assertTrue(app.start_button.instate(["!disabled"]))
        app.image_format.set("PNG")

        with patch("app.messagebox.showinfo"), patch("app.messagebox.showerror") as showerror:
            app.start_button.invoke()
            self.assertTrue(app.preview.use_start_button.instate(["disabled"]))
            self.wait_for(root, lambda: app.open_button.instate(["!disabled"]))
            showerror.assert_not_called()
        exported = sorted(app.result_dir.glob("*.png"))
        self.assertEqual(len(exported), 5)
        for path in exported:
            frame = tk.PhotoImage(master=root, file=str(path))
            red, green, blue = frame.get(0, 0)
            self.assertGreater(green, max(red, blue))

        app._hide_gallery()
        app.preview.reset_button.invoke()
        self.assertEqual(app.preview.portion(), (0.0, 3.0))
        self.assertIn("environ 15 images", app.estimate.get())
        self.choose_source(root, app)
        self.assertEqual(app.preview.portion(), (0.0, 2.0))

    def test_window_updates_estimate_for_interval_mode_and_preserves_fps_mode(self):
        source = self.destination / "vingt secondes.mp4"
        subprocess.run(
            [self.ffmpeg, "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=160x90:rate=30",
             "-t", "20", "-c:v", "mpeg4", str(source)],
            check=True, capture_output=True, timeout=20,
        )
        root, app = self.new_window()
        self.choose_source(root, app, source)
        self.assertIn("environ 100 images", app.estimate.get())

        app.sampling_mode.set("Une image toutes les N secondes")
        app.interval_seconds.set("5")
        self.assertIn("environ 4 images", app.estimate.get())
        self.assertIn("toutes les 5 s", app.estimate.get())
        self.assertTrue(app.start_button.instate(["!disabled"]))

        app.preview.start.set("1")
        app.preview.end.set("3")
        app.preview.add_button.invoke()
        app.preview.start.set("7")
        app.preview.end.set("9")
        app.preview.add_button.invoke()
        self.assertIn("environ 2 images", app.estimate.get())
        self.assertIn("2 passages", app.estimate.get())

        app.sampling_mode.set("Images par seconde")
        self.assertIn("environ 20 images", app.estimate.get())

    def test_engine_rejects_invalid_portions_before_creating_an_export(self):
        for start, end in ((-1, 1), (1, 1), (1.5, 1), (0, 3), (0, float("nan"))):
            with self.subTest(start=start, end=end):
                with self.assertRaisesRegex(FrameCutterError, "début avant la fin"):
                    extract_frames(self.source, self.destination, 5, "JPEG", 2, lambda _: None,
                                   threading.Event(), start=start, end=end)
        self.assertEqual(list(self.destination.iterdir()), [])

    def test_window_exports_several_passages_in_video_order_without_the_gaps(self):
        source = self.destination / "cinq passages.mp4"
        command = [self.ffmpeg, "-v", "error"]
        for color in ("red", "green", "blue", "yellow", "magenta"):
            command += ["-f", "lavfi", "-i", f"color={color}:s=160x90:d=1:r=30"]
        command += ["-filter_complex", "[0:v][1:v][2:v][3:v][4:v]concat=n=5:v=1:a=0",
                    "-c:v", "mpeg4", str(source)]
        subprocess.run(command, check=True, capture_output=True, timeout=15)

        root, app = self.new_window()
        root.deiconify()
        root.geometry("390x844")
        self.choose_source(root, app, source)
        preview = app.preview

        def add(start, end):
            preview.start.set(str(start))
            preview.end.set(str(end))
            preview.add_button.invoke()

        add(4, 5)
        add(1, 2)
        preview.remove_button.invoke()
        add(2, 3)
        add(0, 1)
        add(0.5, 1.5)
        self.assertIn("chevauche", preview._portion_notice.cget("text"))
        self.assertEqual(preview.portions(), ((0, 1), (2, 3), (4, 5)))
        self.assertEqual(preview._portion_list.size(), 3)
        app.fps.set("2")
        self.assertIn("environ 6 images", app.estimate.get())
        self.assertIn("3 passages", app.estimate.get())

        preview.start.set("1")
        preview.end.set("2")
        app.image_format.set("PNG")
        with patch("app.messagebox.showerror") as showerror:
            app.start_button.invoke()
            self.assertTrue(preview.add_button.instate(["disabled"]))
            self.assertTrue(preview.remove_button.instate(["disabled"]))
            self.wait_for(root, lambda: not app.busy)
            showerror.assert_not_called()
        exported = sorted(app.result_dir.glob("*.png"))
        self.assertEqual([file.name for file in exported], [f"frame_{n:06d}.png" for n in range(1, 7)])
        self.assertEqual(app.gallery.frames.size(), 6)
        for file, expected in zip(exported, ["R", "R", "B", "B", "RB", "RB"]):
            frame = tk.PhotoImage(master=root, file=str(file))
            channels = "".join(channel for channel, value in zip("RGB", frame.get(0, 0)) if value > 160)
            self.assertEqual(channels, expected, file.name)

        self.choose_source(root, app)
        self.assertEqual(preview.portions(), ((0, 2),))
        self.assertEqual(preview._portion_list.size(), 0)

    def test_cancelling_a_passage_batch_keeps_the_first_images(self):
        # L’annulation est déclenchée à la fin du premier passage, avant le lancement du second :
        # aucune dépendance à la vitesse de FFmpeg ni à la période de surveillance.
        cancel = threading.Event()
        updates = []
        calls = []
        real_extract_portion = frame_engine._extract_portion

        def first_portion_then_cancel(*args, **kwargs):
            calls.append(args[6:8])
            result = real_extract_portion(*args, **kwargs)
            cancel.set()
            return result

        with patch("frame_engine._extract_portion", side_effect=first_portion_then_cancel), \
                self.assertRaisesRegex(OperationCancelled, "Extraction annulée") as stopped:
            extract_frames(self.source, self.destination, 5, "PNG", 2, updates.append, cancel,
                           portions=((0, 1), (1, 2)))
        self.assertEqual(calls, [(0, 1), (1, 2)])
        output_dir = stopped.exception.output_dir
        self.assertEqual(sorted(p.name for p in output_dir.glob("frame_*.png")),
                         [f"frame_{n:06d}.png" for n in range(1, 6)])
        self.assertTrue(updates)
        self.assertLessEqual(max(updates), 50)

    def test_link_import_only_accepts_single_supported_videos_over_https(self):
        link = parse_video_link("https://youtu.be/abcdefghijk?t=1&list=not-a-video", (60, 65))
        self.assertEqual(link.url, "https://www.youtube.com/watch?v=abcdefghijk")
        self.assertEqual(link.portion, (60, 65))
        reel = parse_video_link("https://instagram.com/reel/abcdefghijk/?igsh=tracking")
        self.assertEqual(reel.platform, "instagram")
        self.assertEqual(reel.url, "https://www.instagram.com/reel/abcdefghijk/")
        for url, bounds, expected in (
            ("file:///C:/video.mp4", None, "lien HTTPS valide"),
            ("https://CANARY_TEST_USER:CANARY_TEST_PASS@example.test/video.mp4", None, "lien HTTPS valide"),
            ("https://www.youtube.com/playlist?list=playlist", None, "vidéo YouTube individuelle"),
            ("https://www.instagram.com/p/abcdefghijk/", None, "Reel Instagram public"),
            ("https://www.instagram.com/reel/abcdefghijk/", (5, 8), "disponibles pour YouTube"),
            ("https://example.invalid/video.mp4", (5, 8), "disponibles pour YouTube"),
        ):
            with self.subTest(url=url):
                with self.assertRaisesRegex(FrameCutterError, expected):
                    parse_video_link(url, bounds)

    def test_output_suggestion_follows_the_video_but_keeps_the_chosen_folder(self):
        root, app = self.new_window()
        other = self.destination / "autre vidéo.mp4"
        shutil.copyfile(self.source, other)

        def select(path):
            with patch("app.filedialog.askopenfilename", return_value=str(path)):
                app.video_button.invoke()
            self.wait_for(root, lambda: not app.busy)
            self.assertTrue(app.start_button.instate(["!disabled"]))

        select(self.source)
        self.assertEqual(Path(app.destination.get()), self.source.parent)
        select(other)
        self.assertEqual(Path(app.destination.get()), other.parent)
        # Choosing the same folder explicitly makes it a fixed destination.
        with patch("app.filedialog.askdirectory", return_value=str(other.parent)):
            app.destination_button.invoke()
        select(self.source)
        self.assertEqual(Path(app.destination.get()), other.parent)

    def test_window_recovers_from_an_interrupted_download(self):
        root, app = self.new_window()
        self.choose_source(root, app)
        link = parse_video_link("https://example.invalid/video.mp4")
        data = self.source.read_bytes()

        class InterruptedResponse(BytesIO):
            def read1(self, size):
                if self.tell() > 12:
                    raise IncompleteRead(b"")
                return super().read1(min(size, 1024))

        responses = [InterruptedResponse(data), BytesIO(data)]
        for response in responses:
            response.status = 200
            response.headers = {"Content-Length": str(len(data))}

        with patch("app.LinkDialog", return_value=SimpleNamespace(result=link)), \
             patch("video_import.build_opener") as build_opener, \
             patch("app.messagebox.showerror") as showerror:
            build_opener.return_value.open.side_effect = responses
            app.link_button.invoke()
            self.wait_for(root, lambda: not app.busy)
            self.assertIn("interrompu", showerror.call_args.args[1])
            self.assertEqual(app.video_path, self.source)
            self.assertTrue(app.start_button.instate(["!disabled"]))

            app.link_button.invoke()
            self.wait_for(root, lambda: not app.busy)
            self.assertEqual(showerror.call_count, 1)
            self.assertEqual(app.video_info.duration, 2.0)
            self.assertEqual(app.video_path.name, "video-importee.mp4")
            self.assertTrue(app.video_path.is_file())

    def test_window_imports_a_direct_video_and_keeps_its_export_after_closing(self):
        root, app = self.new_window()
        link = parse_video_link("https://example.invalid/video.mp4")
        data = self.source.read_bytes()
        response = BytesIO(data)
        response.status = 200
        response.headers = {"Content-Length": str(len(data))}
        opener = SimpleNamespace(open=lambda *_args, **_kwargs: response)
        with patch("app.LinkDialog", return_value=SimpleNamespace(result=link)), \
             patch("video_import.build_opener", return_value=opener), \
             patch("app.messagebox.showerror") as showerror:
            app.link_button.invoke()
            self.assertTrue(app.link_button.instate(["disabled"]))
            self.wait_for(root, lambda: not app.busy)
            showerror.assert_not_called()

        self.assertEqual(app.video_info.duration, 2.0)
        cached = app.video_path
        self.assertTrue(cached.is_file())
        self.assertFalse(Path(app.destination.get()).is_relative_to(cached.parent))
        app.destination.set(str(cached.parent))
        with patch("app.messagebox.showerror") as showerror:
            app.start_button.invoke()
            showerror.assert_called_once()
            self.assertFalse(app.busy)
        app.destination.set(str(self.destination))
        app.start_button.invoke()
        self.wait_for(root, lambda: not app.busy)
        output = app.result_dir
        self.assertEqual(len(list(output.glob("frame_*.jpg"))), 10)
        self.destroy_window(app)
        self.assertFalse(cached.parent.exists())
        self.assertTrue(output.is_dir())

    def test_window_saves_linked_video_with_audio_and_keeps_it_after_close(self):
        root, app = self.new_window()
        self.choose_source(root, app)
        save_to = self.destination / "videos enregistrées"
        save_to.mkdir()
        link = parse_video_link("https://example.invalid/video.mp4")
        data = self.audio_source.read_bytes()
        response = BytesIO(data)
        response.status = 200
        response.headers = {"Content-Length": str(len(data))}
        opener = SimpleNamespace(open=lambda *_args, **_kwargs: response)

        with patch("app.LinkDialog", return_value=SimpleNamespace(result=link, save_to=save_to)), \
                patch("video_import.build_opener", return_value=opener), \
                patch("video_import.MIN_FREE_BYTES", 0), \
                patch("app.messagebox.showerror") as showerror:
            app.link_button.invoke()
            self.wait_for(root, lambda: not app.busy)
            showerror.assert_not_called()

        [saved] = list(save_to.glob("*.mp4"))
        self.assertEqual(saved.read_bytes(), data)
        self.assertEqual(app.video_path, self.source)
        # Nom court seulement : le chemin complet du dossier n’apparaît plus dans la barre d’état.
        self.assertIn(saved.name, app.status.get())
        self.assertNotIn(str(save_to), app.status.get())
        self.destroy_window(app)
        self.assertTrue(saved.is_file())
        streams = subprocess.run(
            [self.ffprobe, "-v", "error", "-show_entries", "stream=codec_type",
             "-of", "csv=p=0", str(saved)],
            check=True, capture_output=True, text=True, timeout=15,
        )
        self.assertIn("audio", streams.stdout.splitlines())

    def test_failed_link_recording_keeps_previous_video_and_cleans_import(self):
        root, app = self.new_window()
        self.choose_source(root, app)
        save_to = self.destination / "failed recording"
        save_to.mkdir()
        link = parse_video_link("https://example.invalid/video.mp4")
        folders = []
        create_folder = app_module.create_import_folder

        def track_folder():
            folder = create_folder()
            folders.append(folder)
            return folder

        with patch("app.LinkDialog", return_value=SimpleNamespace(result=link, save_to=save_to)), \
                patch("app.create_import_folder", side_effect=track_folder), \
                patch("app.download_video", side_effect=FrameCutterError("Échec simulé")), \
                patch("app.messagebox.showerror") as showerror:
            app.link_button.invoke()
            self.wait_for(root, lambda: not app.busy)
            showerror.assert_called_once()

        self.assertEqual(app.video_path, self.source)
        self.assertEqual(list(save_to.iterdir()), [])
        self.assertEqual(len(folders), 1)
        self.assertFalse(folders[0].exists())

    def test_cancelling_link_import_keeps_the_previous_video(self):
        root, app = self.new_window()
        self.choose_source(root, app)
        link = parse_video_link("https://example.invalid/video.mp4")
        folders = []

        def slow_import(_link, folder, cancel):
            folders.append(folder)
            (folder / "part.tmp").touch()
            if not cancel.wait(3):
                raise AssertionError("L’import n’a pas reçu l’annulation.")
            raise OperationCancelled("Import annulé.")

        with patch("app.LinkDialog", return_value=SimpleNamespace(result=link)), \
             patch("app.download_video", side_effect=slow_import), \
             patch("app.messagebox.showerror") as showerror:
            app.link_button.invoke()
            self.wait_for(root, lambda: bool(folders))
            app.cancel_button.invoke()
            self.wait_for(root, lambda: not app.busy and not folders[0].exists())
            showerror.assert_not_called()
        self.assertEqual(app.video_path, self.source)
        self.assertTrue(app.start_button.instate(["!disabled"]))
        self.assertEqual(app.status.get(), "Import annulé.")

    def test_successful_export_opens_the_gallery_and_allows_confirmed_removal(self):
        root, app = self.new_window()
        root.deiconify()
        root.geometry("1040x530")
        original = hashlib.sha256(self.source.read_bytes()).digest()
        self.choose_source(root, app)
        self.assertTrue(app.gallery_button.instate(["disabled"]))
        with patch("app.messagebox.showinfo") as showinfo, patch("app.messagebox.showerror") as showerror:
            app.start_button.invoke()
            self.wait_for(root, lambda: app.gallery_button.instate(["!disabled"]))
            showerror.assert_not_called()
        self.assertTrue(app._gallery_visible, "La galerie doit apparaître dès que l’export est terminé.")
        showinfo.assert_not_called()
        output = app.result_dir
        untouched = output / "note.txt"
        untouched.write_text("Autre fichier", encoding="utf-8")
        selected = output / "frame_000002.jpg"
        gallery = app.gallery
        try:
            self.wait_for(root, lambda: gallery.delete_button.instate(["!disabled"]))
            self.assertTrue(gallery.winfo_viewable())
            self.assertIs(gallery.winfo_toplevel(), root)
            self.assertTrue(app.preview.winfo_viewable())
            self.assertIsNotNone(gallery._photo)
            button_y = app.gallery_button.winfo_rooty()
            self.assertEqual(app.canvas.yview()[0], 0)
            app.preview.picture.event_generate("<MouseWheel>", delta=-120)
            root.update()
            self.assertGreater(app.canvas.yview()[0], 0)
            app.gallery_button.invoke()
            root.update()
            self.assertEqual(app.gallery_button.winfo_rooty(), button_y)
            self.assertGreaterEqual(gallery.winfo_rooty(), app.canvas.winfo_rooty() - 1)
            self.assertLess(gallery.winfo_rooty(), app.canvas.winfo_rooty() + app.canvas.winfo_height())

            screen_w = root.winfo_screenwidth()
            wide_width = 1650 if screen_w >= 1700 else max(960, screen_w - 40)
            root.geometry(f"{wide_width}x800")
            self.wait_for(root, lambda: root.winfo_width() >= wide_width - 16 and gallery._photo is not None)
            self.assertEqual(app.form.winfo_rooty(), app.preview.winfo_rooty())
            self.assertGreater(gallery.winfo_rooty(), app.preview.winfo_rooty() + app.preview.winfo_height())
            self.assertEqual(gallery.winfo_rootx(), app.form.winfo_rootx())
            self.assertEqual(gallery.winfo_rootx() + gallery.winfo_width(),
                             app.preview.winfo_rootx() + app.preview.winfo_width())
            self.assertAlmostEqual(gallery.winfo_rootx() + gallery.winfo_width() / 2,
                                   root.winfo_rootx() + root.winfo_width() / 2, delta=8)
            wide_image_width = gallery._photo.width()

            root.geometry("390x844")
            self.wait_for(root, lambda: not app.preview.winfo_viewable() and gallery._photo is not None
                          and gallery._photo.width() < wide_image_width)
            self.assertLess(gallery.winfo_rooty(), app.form.winfo_rooty())
            self.assertEqual(gallery.frames.size(), 10)
            gallery.next_button.invoke()
            self.wait_for(root, lambda: gallery.delete_button.instate(["!disabled"]))
            self.assertIn(selected.name, gallery._caption.get())

            gallery.delete_button.invoke()
            self.assertIn(selected.name, gallery._question.get())
            self.assertIs(gallery.confirm_delete_button.winfo_toplevel(), root)
            gallery.cancel_delete_button.invoke()
            self.assertTrue(selected.exists())

            gallery.delete_button.invoke()
            gallery.confirm_delete_button.invoke()
            self.assertFalse(selected.exists())
            self.assertEqual(gallery.frames.size(), 9)
            self.assertIn("frame_000003.jpg", gallery._caption.get())
            self.assertTrue(untouched.is_file())
            self.assertTrue((output / "frame_000001.jpg").is_file())
            self.assertEqual(hashlib.sha256(self.source.read_bytes()).digest(), original)
        finally:
            app._hide_gallery()
            root.update()

        button_bottom = app.gallery_button.winfo_rooty() + app.gallery_button.winfo_height()
        self.assertTrue(app.gallery_button.winfo_viewable())
        self.assertLessEqual(button_bottom, root.winfo_rooty() + root.winfo_height())
        app.gallery_button.invoke()
        self.assertTrue(app._gallery_visible)
        self.assertEqual(app.gallery.frames.size(), 9)

    def test_gallery_displays_png_and_handles_an_empty_export(self):
        output = extract_frames(self.source, self.destination, 5, "PNG", 2, lambda _: None,
                                threading.Event(), end=0.2)
        root, app = self.new_window()
        app.result_dir = output
        app._set_busy_controls(False)
        app.gallery_button.invoke()
        gallery = app.gallery
        self.assertTrue(app._gallery_visible)
        try:
            self.wait_for(root, lambda: gallery.delete_button.instate(["!disabled"]))
            self.assertIsNotNone(gallery._photo)
            self.assertEqual(gallery.frames.size(), 1)
            selected = output / "frame_000001.png"
            saved = output / "copie.png"

            gallery.delete_button.invoke()
            selected.rename(saved)
            shutil.copyfile(saved, selected)
            gallery.confirm_delete_button.invoke()
            self.assertIn("Suppression impossible", gallery._notice.cget("text"))
            self.assertTrue(selected.is_file())

            gallery.delete_button.invoke()
            gallery.confirm_delete_button.invoke()
            self.assertFalse(selected.exists())
            self.assertTrue(saved.is_file())
            self.assertEqual(gallery.frames.size(), 0)
            self.assertIn("Aucune image", gallery.picture.cget("text"))
            self.assertTrue(gallery.delete_button.instate(["disabled"]))
        finally:
            app._hide_gallery()

    def test_window_stops_the_gallery_decoder_before_closing(self):
        root, app = self.new_window()
        app.result_dir = self.extract()
        app._set_busy_controls(False)
        closed = threading.Event()
        root.bind("<Destroy>", lambda event: closed.set() if event.widget is root else None)
        popen = subprocess.Popen
        decoders = []

        def slow_decoder(_command, **options):
            process = popen([sys.executable, "-B", "-c", "import time; time.sleep(15)"], **options)
            decoders.append(process)
            return process

        with patch("frame_engine.subprocess.Popen", side_effect=slow_decoder):
            try:
                app.gallery_button.invoke()
                self.wait_for(root, lambda: bool(decoders))
                self.assertIsNone(decoders[0].poll())
                root.tk.call(root.protocol("WM_DELETE_WINDOW"))
                self.wait_for(root, closed.is_set, timeout=3)
                self.assertIsNotNone(decoders[0].poll())
            finally:
                for process in decoders:
                    if process.poll() is None:
                        process.kill()
                    process.wait(timeout=3)

    def test_window_ignores_late_errors_and_finishes_extraction(self):
        root, app = self.new_window()
        self.choose_source(root, app)
        with patch("app.messagebox.showinfo"), patch("app.messagebox.showerror") as showerror:
            app.events.put((app._operation_id, "error", FrameCutterError("Analyse périmée")))
            app.start_button.invoke()
            self.wait_for(root, lambda: app.open_button.instate(["!disabled"]))
            self.assertEqual(len(list(app.result_dir.glob("*.jpg"))), 10)
            showerror.assert_not_called()

    def test_window_cancels_and_closes_during_real_extraction(self):
        for close in (False, True):
            with self.subTest(close=close):
                self.check_window_stop(close)

    def test_window_closes_during_video_analysis(self):
        root, app = self.new_window()
        closed = threading.Event()
        root.bind("<Destroy>", lambda event: closed.set() if event.widget is root else None)
        popen = subprocess.Popen
        processes = []

        def slow_probe(_command, **options):
            process = popen([sys.executable, "-B", "-c", "import time; time.sleep(15)"], **options)
            processes.append(process)
            return process

        with patch("frame_engine.subprocess.Popen", side_effect=slow_probe), \
             patch("app.filedialog.askopenfilename", return_value=str(self.source)):
            try:
                app.video_button.invoke()
                self.wait_for(root, lambda: bool(processes))
                self.assertIsNone(processes[0].poll())
                root.tk.call(root.protocol("WM_DELETE_WINDOW"))
                self.wait_for(root, closed.is_set, timeout=3)
                self.assertIsNotNone(processes[0].poll())
            finally:
                for process in processes:
                    if process.poll() is None:
                        process.kill()
                    process.wait(timeout=3)

    def check_window_stop(self, close):
        root, app = self.new_window()
        self.choose_source(root, app)
        popen = subprocess.Popen
        processes = []

        def paced_ffmpeg(command, **options):
            command = list(command)
            input_position = command.index("-i")
            command[input_position:input_position] = ["-stream_loop", "4", "-re"]
            process = popen(command, **options)
            processes.append(process)
            return process

        closed = threading.Event()
        root.bind("<Destroy>", lambda event: closed.set() if event.widget is root else None)
        before = set(self.destination.iterdir())

        def has_partial_images():
            exports = set(self.destination.iterdir()) - before
            return any(list(path.glob("*.jpg")) for path in exports)

        with patch("frame_engine.subprocess.Popen", side_effect=paced_ffmpeg), \
             patch("app.messagebox.showinfo"), patch("app.messagebox.showerror") as showerror:
            try:
                app.start_button.invoke()
                self.wait_for(root, has_partial_images)
                self.assertTrue(app.start_button.instate(["disabled"]))
                self.assertIsNone(processes[0].poll())
                if close:
                    root.tk.call(root.protocol("WM_DELETE_WINDOW"))
                    self.wait_for(root, closed.is_set, timeout=15)
                else:
                    app.cancel_button.invoke()
                    self.wait_for(root, lambda: app.open_button.instate(["!disabled"]), timeout=15)
                    self.assertIn("annulée", app.status.get())
                    self.assertFalse(app._gallery_visible)
                    self.assertTrue(list(app.result_dir.glob("*.jpg")))
                self.assertIsNotNone(processes[0].poll(), "FFmpeg est encore actif.")
                self.assertTrue(has_partial_images())
                showerror.assert_not_called()
            finally:
                for process in processes:
                    if process.poll() is None:
                        process.kill()
                    process.wait(timeout=10)

    def test_open_result_uses_absolute_system_explorer_and_resolved_path(self):
        root, app = self.new_window()
        export_dir = self.destination / "export_dir"
        export_dir.mkdir()
        relative_dir = Path(os.path.relpath(export_dir, Path.cwd()))
        app.result_dir = relative_dir
        expected_explorer = str(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "explorer.exe")
        with patch("app.subprocess.Popen") as popen:
            app._open_result()
            popen.assert_called_once()
            command = popen.call_args.args[0]
            self.assertEqual(command[0], expected_explorer)
            self.assertNotEqual(command[0].lower(), "explorer")
            self.assertEqual(command[1], str(export_dir.resolve()))

    def test_leading_dash_relative_paths_work_without_cli_option_injection(self):
        dash_video = self.destination / "-v.mp4"
        shutil.copyfile(self.source, dash_video)
        dash_out = self.destination / "-out"
        dash_out.mkdir()
        previous_cwd = Path.cwd()
        os.chdir(self.destination)
        try:
            info = probe_video(Path("-v.mp4"))
            self.assertEqual((info.duration, info.width, info.height), (2, 160, 90))
            preview = read_preview_frame(Path("-v.mp4"), 0, 160, 90, threading.Event())
            self.assertTrue(preview.startswith(b"\x89PNG\r\n\x1a\n"))
            output = extract_frames(Path("-v.mp4"), Path("-out"), 5, "JPEG", 2, lambda _: None, threading.Event())
            self.assertEqual(output, (dash_out / "-v_frames").resolve())
            self.assertEqual(len(list(output.glob("frame_*.jpg"))), 10)
        finally:
            os.chdir(previous_cwd)

    def test_available_output_dir_rejects_path_traversal_stems(self):
        sub = self.destination / "confined"
        sub.mkdir()
        for bad_stem in ("..\\escaped", "../escaped", "nested/dir", "nested\\dir", "..", ".", "", "CANARY_TEST\x00nul"):
            with self.subTest(bad_stem=bad_stem):
                with self.assertRaisesRegex(FrameCutterError, "Nom de dossier de résultat invalide"):
                    _available_output_dir(sub, bad_stem)
        self.assertEqual(list(sub.iterdir()), [])
        self.assertFalse((self.destination / "escaped_frames").exists())

    def test_failed_extraction_on_corrupt_video_cleans_empty_output_dir(self):
        corrupt = self.destination / "corrompue.mp4"
        corrupt.write_bytes(b"")
        with self.assertRaisesRegex(FrameCutterError, "L’extraction a échoué"):
            extract_frames(corrupt, self.destination, 5, "JPEG", 2, lambda _: None, threading.Event())
        self.assertEqual(list(self.destination.iterdir()), [corrupt])

    def test_format_duration_handles_non_finite_values(self):
        for value in (float("nan"), float("inf"), float("-inf"), -5.0):
            with self.subTest(value=value):
                self.assertEqual(format_duration(value), "00:00")


class ReleaseHardeningTests(unittest.TestCase):
    CANARY_WIN = r"C:\Users\CANARY_TEST_USER\Videos\secret_clip.mp4"
    CANARY_WIN_D = r"D:\CANARY_TEST_DIR\secret_clip.mp4"
    CANARY_WIN_E = r"E:\Users\CANARY_TEST_USER\Exports\secret_clip_frames"
    CANARY_UNIX = "/Users/CANARY_TEST_USER/Videos/secret_clip.mp4"
    CANARY_LINUX = "/home/CANARY_TEST_USER/secret_clip.mp4"

    def assert_no_system_leak(self, text: str) -> None:
        self.assertRegex(text.strip(), r"\S")
        self.assertNotRegex(text, r"[A-Za-z]:[\\/]")
        self.assertNotIn("\\\\", text)
        self.assertNotIn("/Users/", text)
        self.assertNotIn("/home/", text)
        self.assertNotIn("CANARY_TEST_USER", text)
        self.assertNotIn("CANARY_TEST_DIR", text)
        self.assertNotIn("secret_clip.mp4", text)
        self.assertNotIn("secret_clip_frames", text)
        self.assertNotRegex(text, r"\[[^\]]+@\s*(?:0x)?[0-9a-fA-F]+\]")
        self.assertNotIn("Error opening input", text)

    def test_sanitize_user_error_strips_paths_and_ffmpeg_details(self):
        from frame_engine import sanitize_user_error

        samples = (
            f"Impossible d'ouvrir '{self.CANARY_WIN}' : accès refusé",
            f"Erreur sur {self.CANARY_WIN_D} et {self.CANARY_WIN_E}",
            f"Lecture impossible pour {self.CANARY_UNIX} ou {self.CANARY_LINUX}",
            f"Lien local refusé : file:///{self.CANARY_WIN.replace(chr(92), '/')}",
            (
                "Impossible de lire cette vidéo.\n"
                "[http @ 000001f47d198080] Protocol 'http' not on whitelist 'file'!\n"
                f"Error opening input file {self.CANARY_WIN}.\n"
                f"{self.CANARY_WIN}: Invalid data found when processing input"
            ),
        )
        for sample in samples:
            with self.subTest(sample=sample):
                cleaned = sanitize_user_error(sample)
                self.assert_no_system_leak(cleaned)

        kept = sanitize_user_error(
            "Téléchargement gratuit : https://ffmpeg.org/download.html"
        )
        self.assertIn("https://ffmpeg.org/download.html", kept)

    def test_engine_errors_hide_system_paths_and_raw_ffmpeg_stderr(self):
        # Découplé : vrais fichiers temporaires, aucun remplacement global de Path.
        # Seul le processus FFmpeg est simulé, et chaque branche d’erreur reçoit un stderr
        # qui contient le chemin réel et un chemin Windows fictif.
        branches = (
            ("No space left on device", "Espace disque insuffisant", "Espace disque insuffisant"),
            ("Protocol 'http' not on whitelist 'file'!", "source externe non prise en charge",
             "source externe non prise en charge"),
            ("Format not on whitelist 'mov,matroska'", "format de fichier non pris en charge",
             "format de fichier non pris en charge"),
            ("Permission denied", "accès au fichier refusé", "accès au fichier ou au dossier refusé"),
            ("moov atom not found", "Impossible de lire cette vidéo", "L’extraction a échoué. Vérifiez"),
        )
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "CANARY_TEST_USER").mkdir()
            source = root / "CANARY_TEST_USER" / "secret_clip.mp4"
            source.write_bytes(b"CANARY_TEST")
            destination = root / "CANARY_TEST_DIR"
            destination.mkdir()

            def fake_process(stderr_text):
                class Process:
                    returncode = 1
                    stdout = iter(())
                    stderr = iter(stderr_text.splitlines(keepends=True))

                    def communicate(self, timeout=None):
                        return "", stderr_text

                    def wait(self):
                        return 1

                @contextmanager
                def run(*_args, **_kwargs):
                    yield Process()

                return run

            with patch("frame_engine.require_ffmpeg", return_value=("ffmpeg", "ffprobe")):
                for token, probe_message, extract_message in branches:
                    stderr_text = (
                        f"[in#0 @ 00000123456789ab] Error opening input: {token}\n"
                        f"Error opening input file {source}.\n"
                        f"{self.CANARY_WIN}: {token}\n"
                        f"{source}: {token}\n"
                    )
                    with self.subTest(branch=token), \
                            patch("frame_engine._video_process", side_effect=fake_process(stderr_text)):
                        with self.assertRaisesRegex(FrameCutterError, probe_message) as ctx:
                            probe_video(source)
                        self.assert_no_system_leak(str(ctx.exception))
                        with self.assertRaisesRegex(FrameCutterError, extract_message) as ctx:
                            extract_frames(source, destination, 5, "JPEG", 2.0, lambda _: None, threading.Event())
                        self.assert_no_system_leak(str(ctx.exception))
                        self.assertEqual(list(destination.iterdir()), [])

                launch_failure = OSError(errno.EACCES, "Permission denied", self.CANARY_WIN)
                for run, message in (
                    (lambda: probe_video(source), "Impossible de lancer l’outil d’analyse"),
                    (lambda: read_preview_frame(source, 1.0, 160, 90, threading.Event()), "Impossible de lancer l’aperçu"),
                    (lambda: extract_frames(source, destination, 5, "JPEG", 2.0, lambda _: None, threading.Event()),
                     "Impossible de lancer le composant vidéo"),
                ):
                    with self.subTest(launch=message), patch("frame_engine.subprocess.Popen", side_effect=launch_failure):
                        with self.assertRaisesRegex(FrameCutterError, message) as ctx:
                            run()
                        self.assert_no_system_leak(str(ctx.exception))

            with patch.object(Path, "mkdir", side_effect=PermissionError(errno.EACCES, "Permission denied", self.CANARY_WIN_E)):
                with self.assertRaisesRegex(FrameCutterError, "Choisissez un dossier accessible") as ctx:
                    _available_output_dir(destination, "clip")
            self.assert_no_system_leak(str(ctx.exception))

    def test_app_error_messages_hide_system_paths_and_os_details(self):
        root = tk.Tk()
        root.withdraw()
        app = FrameCutterApp(root)
        try:
            op_id = app._begin_operation()
            with patch("app.probe_video", side_effect=RuntimeError(f"Crash at {self.CANARY_WIN}")):
                app._probe_worker(op_id, Path(self.CANARY_WIN))
            with patch("app.messagebox.showerror") as showerror:
                app._poll_events()
                showerror.assert_called_once()
                self.assert_no_system_leak(showerror.call_args.args[1])
                self.assert_no_system_leak(app.status.get())

            op_id = app._begin_operation()
            with patch("app.extract_frames", side_effect=OSError(errno.EACCES, "Permission denied", self.CANARY_WIN_D)):
                app._extract_worker(op_id, Path(self.CANARY_WIN), Path(self.CANARY_WIN_E), 5.0, "JPEG", 2.0, ((0.0, 2.0),), None)
            with patch("app.messagebox.showerror") as showerror:
                app._poll_events()
                showerror.assert_called_once()
                self.assert_no_system_leak(showerror.call_args.args[1])
                self.assert_no_system_leak(app.status.get())

            op_id = app._begin_operation()
            app.events.put((op_id, "error", FrameCutterError(f"Échec sur {self.CANARY_WIN}", output_dir=Path(self.CANARY_WIN_E))))
            with patch("app.messagebox.showerror") as showerror:
                app._poll_events()
                showerror.assert_called_once()
                self.assert_no_system_leak(showerror.call_args.args[1])
                self.assert_no_system_leak(app.status.get())

            app.result_dir = Path(self.CANARY_WIN_E)
            app.busy = False
            with patch.object(app.gallery, "load", side_effect=PermissionError(errno.EACCES, "Permission denied", self.CANARY_WIN_E)), \
                    patch.object(app.gallery, "show_error") as show_error:
                app._show_gallery(reveal=False)
                show_error.assert_called_once()
                self.assert_no_system_leak(show_error.call_args.args[0])

            with patch.object(Path, "is_dir", return_value=True), \
                    patch.object(Path, "resolve", return_value=Path(self.CANARY_WIN_E)), \
                    patch("app.subprocess.Popen", side_effect=FileNotFoundError(errno.ENOENT, "Not found", self.CANARY_WIN)), \
                    patch("app.messagebox.showerror") as showerror:
                app._open_result()
                showerror.assert_called_once()
                self.assert_no_system_leak(showerror.call_args.args[1])
        finally:
            if app._poll_job is not None:
                root.after_cancel(app._poll_job)
                app._poll_job = None
            app.preview.clear()
            app.gallery.destroy()
            root.destroy()

    def test_requirements_import_pins_versions_and_valid_sha256_hashes(self):
        project = Path(__file__).resolve().parents[1]
        req_file = project / "requirements-import.txt"
        self.assertTrue(req_file.is_file())
        raw_lines = req_file.read_text(encoding="utf-8").splitlines()
        logical_lines = []
        current = ""
        for raw in raw_lines:
            stripped = raw.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if stripped.endswith("\\"):
                current += " " + stripped[:-1].strip()
            else:
                current += " " + stripped
                logical_lines.append(current.strip())
                current = ""
        if current.strip():
            logical_lines.append(current.strip())

        self.assertGreaterEqual(len(logical_lines), 2)
        seen_packages = set()
        for entry in logical_lines:
            spec = entry.split("--hash=", 1)[0].strip()
            self.assertRegex(spec, r"^[A-Za-z0-9_.-]+==[A-Za-z0-9_.-]+$")
            seen_packages.add(spec.split("==", 1)[0].lower())
            hashes = re.findall(r"--hash=sha256:([0-9a-fA-F]+)", entry)
            self.assertGreaterEqual(len(hashes), 1, f"Missing sha256 hash in: {entry}")
            for digest in hashes:
                self.assertEqual(len(digest), 64)
                self.assertRegex(digest, r"^[0-9a-f]{64}$")

        self.assertIn("yt-dlp", seen_packages)
        self.assertIn("yt-dlp-ejs", seen_packages)

    @unittest.skipUnless(os.environ.get("FRAME_CUTTER_NETWORK_TESTS") == "1",
                         "Test réseau (PyPI) : définir FRAME_CUTTER_NETWORK_TESTS=1 pour l’exécuter.")
    def test_requirements_import_hashes_resolve_on_pypi(self):
        req_file = Path(__file__).resolve().parents[1] / "requirements-import.txt"
        dry_run = subprocess.run(
            [
                sys.executable, "-m", "pip", "install",
                "--dry-run", "--disable-pip-version-check",
                "--ignore-installed", "--require-hashes",
                "-r", str(req_file),
            ],
            capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(dry_run.returncode, 0, dry_run.stderr or dry_run.stdout)

    def test_ci_workflow_is_read_only_and_pins_actions_to_full_commit_sha(self):
        project = Path(__file__).resolve().parents[1]
        workflow_path = project / ".github" / "workflows" / "tests.yml"
        self.assertTrue(workflow_path.is_file(), "Le workflow CI .github/workflows/tests.yml est manquant.")
        content = workflow_path.read_text(encoding="utf-8")
        self.assertIn("push:", content)
        self.assertIn("pull_request:", content)
        self.assertIn("main", content)
        self.assertRegex(content, r"permissions:\s*\n\s+contents:\s*read\b")
        self.assertNotRegex(content, r":\s*write\b")

        uses_refs = re.findall(r"^\s*-?\s*uses:\s*([^\s#]+)", content, flags=re.MULTILINE)
        self.assertGreaterEqual(len(uses_refs), 2)
        for ref in uses_refs:
            self.assertRegex(
                ref,
                r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+@[0-9a-f]{40}$",
                f"Action must be pinned to a full 40-char commit SHA: {ref}",
            )

        self.assertIn("python -B -W default -m unittest discover -s tests -v", content)
        self.assertIn("--require-hashes", content)
