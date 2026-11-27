"""A small Windows desktop interface for extracting video frames locally."""

from __future__ import annotations

import logging
import math
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from dataclasses import dataclass
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from export_gallery import ExportGallery
from frame_engine import (
    FrameCutterError, OperationCancelled, VideoInfo, extract_frames, format_duration, format_os_error,
    log_unexpected_error, probe_video, sanitize_user_error,
)
from link_dialog import LinkDialog
from ui_style import BACKGROUND, L, M, S, XL, XS, apply_style, step_heading
from video_import import VideoLink, clean_abandoned_imports, create_import_folder, download_video, remove_import_folder, save_video_copy
from video_preview import VideoPreview


APP_NAME = "Frame Cutter"
LARGE_EXPORT_COUNT = 10_000
WORKSPACE_MAX_WIDTH = 1280
FPS_MODE = "Images par seconde"
INTERVAL_MODE = "Une image toutes les N secondes"
VIDEO_TYPES = [
    ("Vidéos", "*.mp4 *.mov *.mkv *.avi *.webm *.m4v *.mpeg *.mpg *.wmv *.ts"),
    ("Tous les fichiers", "*.*"),
]


@dataclass(frozen=True)
class ImportedSource:
    path: Path
    info: VideoInfo
    folder: Path
    is_excerpt: bool


class FrameCutterApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title(APP_NAME)
        self.root.geometry("1140x700")
        self.root.minsize(390, 530)

        self.video_path: Path | None = None
        self.video_info: VideoInfo | None = None
        self._import_folder: Path | None = None
        self.destination = tk.StringVar()
        self._suggested_destination: str | None = None
        self.sampling_mode = tk.StringVar(value=FPS_MODE)
        self.fps = tk.StringVar(value="5")
        self.interval_seconds = tk.StringVar(value="5")
        self.image_format = tk.StringVar(value="JPEG")
        self.status = tk.StringVar(value="Choisissez une vidéo pour commencer.")
        self.video_details = tk.StringVar(value="Aucune vidéo sélectionnée.")
        self.estimate = tk.StringVar(value="")
        self.events: queue.Queue[tuple[int, str, object]] = queue.Queue()
        self.cancel_event = threading.Event()
        self.busy = False
        self._operation_id = 0
        self._closing = False
        self.result_dir: Path | None = None
        self._gallery_visible = False

        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self._close)
        self._poll_job: str | None = self.root.after(100, self._poll_events)

    def _build_ui(self) -> None:
        apply_style(self.root)
        self.header = ttk.Frame(self.root, style="Canvas.TFrame")
        self.header.pack(fill="x", padx=L, pady=(L, M))
        ttk.Label(self.header, text=APP_NAME, style="Title.TLabel").pack(anchor="w")
        ttk.Label(self.header, text="Transformez un passage vidéo en images.", style="Subtitle.TLabel").pack(anchor="w", pady=(XS, 0))

        self.dock = ttk.Frame(self.root, padding=(L, M))
        self.dock.pack(side="bottom", fill="x", padx=L, pady=(S, L))
        self.actions = ttk.Frame(self.dock)
        self.actions.pack(fill="x")
        self.start_button = ttk.Button(self.actions, text="Extraire les images", style="Primary.TButton",
                                       command=self._start_extraction, state="disabled")
        self.cancel_button = ttk.Button(self.actions, text="Annuler", style="Quiet.TButton",
                                        command=self._cancel, state="disabled")
        self.gallery_button = ttk.Button(self.actions, text="Voir les images", command=self._show_gallery, state="disabled")
        self.open_button = ttk.Button(self.actions, text="Ouvrir le dossier", style="Quiet.TButton",
                                      command=self._open_result, state="disabled")
        self.status_label = ttk.Label(self.dock, textvariable=self.status, style="Meta.TLabel")
        self.status_label.pack(anchor="w", pady=(S, XS))
        self.progress = ttk.Progressbar(self.dock, mode="determinate", maximum=100)
        self.progress.pack(fill="x")

        viewport = ttk.Frame(self.root, style="Canvas.TFrame")
        viewport.pack(fill="both", expand=True, padx=L)
        viewport.rowconfigure(0, weight=1)
        viewport.columnconfigure(0, weight=1)
        viewport.columnconfigure(1, minsize=M)
        self.canvas = tk.Canvas(viewport, background=BACKGROUND, borderwidth=0, highlightthickness=0)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.scrollbar = ttk.Scrollbar(viewport, orient="vertical", command=self.canvas.yview)
        self.scrollbar.grid(row=0, column=1, sticky="ns")
        self.canvas.configure(yscrollcommand=self.scrollbar.set)

        self.workspace = ttk.Frame(self.canvas, style="Canvas.TFrame")
        self._workspace_window = self.canvas.create_window(0, 0, window=self.workspace, anchor="nw")
        self.form = ttk.Frame(self.workspace, padding=L)
        self._build_source(self.form)
        self.preview = VideoPreview(self.workspace, self._update_estimate)
        self.export_settings = ttk.Frame(self.workspace, padding=L)
        self._build_export_settings(self.export_settings)
        self.gallery = ExportGallery(self.workspace, self._hide_gallery)
        self._compact: bool | None = None
        self.workspace.bind("<Configure>", self._sync_scroll)
        self.canvas.bind("<Configure>", self._resize_workspace)
        self.root.bind("<MouseWheel>", self._scroll_workspace)
        self.root.bind("<FocusIn>", self._reveal_focus)
        self._layout_workspace(1080)
        self.fps.trace_add("write", lambda *_: self._update_estimate())
        self.interval_seconds.trace_add("write", lambda *_: self._update_estimate())
        self.sampling_mode.trace_add("write", lambda *_: self._update_estimate())

    def _build_source(self, form: ttk.Frame) -> None:
        step_heading(form, "01", "Choisir la vidéo").pack(anchor="w")
        self.video_button = ttk.Button(form, text="Ouvrir un fichier vidéo…", style="Primary.TButton",
                                       command=self._choose_video)
        self.video_button.pack(fill="x", pady=(M, 0))
        self.link_button = ttk.Button(form, text="Importer un lien…", command=self._choose_link)
        self.link_button.pack(fill="x", pady=(S, 0))
        self.details_label = ttk.Label(form, textvariable=self.video_details, style="Meta.TLabel", wraplength=288)
        self.details_label.pack(fill="x", pady=(S, 0))

    def _build_export_settings(self, form: ttk.Frame) -> None:
        step_heading(form, "03", "Régler l’export").pack(anchor="w")
        settings = ttk.Frame(form)
        settings.pack(fill="x", pady=(M, 0))
        settings.columnconfigure((0, 1), weight=1, uniform="settings")
        ttk.Label(settings, text="Mode de cadence").grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, XS))
        self.sampling_mode_combo = ttk.Combobox(
            settings, textvariable=self.sampling_mode, values=(FPS_MODE, INTERVAL_MODE),
            state="readonly", width=30,
        )
        self.sampling_mode_combo.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(0, M))
        self.rate_label = ttk.Label(settings, text="Images par seconde")
        self.rate_label.grid(row=2, column=0, sticky="w", padx=(0, S), pady=(0, XS))
        ttk.Label(settings, text="Format").grid(row=2, column=1, sticky="w", pady=(0, XS))
        self.fps_entry = ttk.Spinbox(settings, from_=1, to=60, increment=1, textvariable=self.fps, width=5,
                                     command=self._update_estimate)
        self.fps_entry.grid(row=3, column=0, sticky="ew", padx=(0, S))
        self.interval_entry = ttk.Spinbox(
            settings, from_=1, to=3600, increment=1, textvariable=self.interval_seconds, width=5,
            command=self._update_estimate,
        )
        self.format_combo = ttk.Combobox(settings, textvariable=self.image_format, values=("JPEG", "PNG"),
                                        state="readonly", width=7)
        self.format_combo.grid(row=3, column=1, sticky="ew")
        self._update_sampling_controls()

        ttk.Label(form, text="Dossier de sortie").pack(anchor="w", pady=(M, XS))
        output = ttk.Frame(form)
        output.pack(fill="x")
        self.destination_entry = ttk.Entry(output, textvariable=self.destination, width=12)
        self.destination_entry.pack(side="left", fill="x", expand=True)
        self.destination_button = ttk.Button(output, text="Parcourir…", command=self._choose_destination)
        self.destination_button.pack(side="right", padx=(S, 0))
        ttk.Label(form, textvariable=self.estimate, style="Meta.TLabel", wraplength=288).pack(fill="x", pady=(M, 0))

    def _layout_workspace(self, width: int) -> None:
        compact = width < 720
        if compact == self._compact:
            return
        self._compact = compact
        self.workspace.columnconfigure(0, weight=1 if compact else 0, minsize=0 if compact else 352)
        self.workspace.columnconfigure(1, weight=0 if compact else 1)
        self.workspace.rowconfigure(1, weight=0 if compact else 1)
        self._place_workspace_panels()
        for button in (self.start_button, self.cancel_button, self.gallery_button, self.open_button):
            button.grid_forget()
        for column in range(5):
            self.actions.columnconfigure(column, weight=0)
        if compact:
            self.actions.columnconfigure((0, 1), weight=1, uniform="actions")
            self.start_button.grid(row=0, column=0, sticky="ew", padx=(0, S))
            self.cancel_button.grid(row=0, column=1, sticky="ew")
            self.gallery_button.grid(row=1, column=0, sticky="ew", padx=(0, S), pady=(S, 0))
            self.open_button.grid(row=1, column=1, sticky="ew", pady=(S, 0))
        else:
            self.actions.columnconfigure((0, 1), uniform="")
            self.actions.columnconfigure(2, weight=1)
            self.start_button.grid(row=0, column=0, padx=(0, S))
            self.cancel_button.grid(row=0, column=1)
            self.gallery_button.grid(row=0, column=3, padx=(0, S))
            self.open_button.grid(row=0, column=4)

    def _place_workspace_panels(self) -> None:
        results_first = bool(self._compact and self._gallery_visible)
        self.form.grid(row=1 if results_first else 0, column=0, sticky="new",
                       pady=(L, 0) if results_first else (0, 0))
        self.export_settings.grid(row=2 if self._compact else 1, column=0, sticky="new", pady=(L, 0))
        if results_first:
            self.preview.grid_remove()
        else:
            self.preview.grid(row=1 if self._compact else 0, column=0 if self._compact else 1,
                              rowspan=1 if self._compact else 2, sticky="new",
                              padx=(0, 0) if self._compact else (L, 0),
                              pady=(L, 0) if self._compact else (0, 0))
        if self._gallery_visible:
            self.gallery.grid(row=0 if results_first else 2, column=0, columnspan=1 if self._compact else 2,
                              sticky="new", pady=(0 if results_first else XL, 0))
        else:
            self.gallery.grid_remove()
        self._update_media()

    def _resize_workspace(self, event: tk.Event) -> None:
        width = min(event.width, WORKSPACE_MAX_WIDTH)
        offset = (event.width - width) // 2
        self.canvas.itemconfigure(self._workspace_window, width=width)
        self.canvas.coords(self._workspace_window, offset, 0)
        margins = (L + offset, L + M + event.width - width - offset)
        self.header.pack_configure(padx=margins)
        self.dock.pack_configure(padx=margins)
        self.status_label.configure(wraplength=max(240, width - 2 * L))
        self._layout_workspace(width)
        self._sync_scroll()

    def _sync_scroll(self, _event: tk.Event | None = None) -> None:
        visible_height = self.canvas.winfo_height()
        height = max(visible_height, self.workspace.winfo_height())
        self.canvas.configure(scrollregion=(0, 0, self.canvas.winfo_width(), height))
        if height > visible_height:
            self.scrollbar.grid()
        else:
            self.scrollbar.grid_remove()
            self.canvas.yview_moveto(0)

    def _scroll_workspace(self, event: tk.Event) -> str | None:
        if isinstance(event.widget, (tk.Listbox, ttk.Spinbox, ttk.Combobox)):
            return None
        if self.canvas.yview() != (0.0, 1.0):
            self.canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")
            return "break"
        return None

    def _reveal_focus(self, event: tk.Event) -> None:
        widget = event.widget
        parent = widget
        while parent is not None and parent is not self.workspace:
            parent = getattr(parent, "master", None)
        if parent is self.workspace:
            self._reveal(widget)

    def _reveal(self, widget: tk.Widget) -> None:
        height = self.workspace.winfo_height()
        visible_height = self.canvas.winfo_height()
        if height <= visible_height or not widget.winfo_ismapped():
            return
        offset = widget.winfo_rooty() - self.canvas.winfo_rooty()
        top = self.canvas.canvasy(0)
        if offset < 0:
            self.canvas.yview_moveto((top + offset) / height)
        elif offset + min(widget.winfo_height(), visible_height) > visible_height:
            self.canvas.yview_moveto((top + offset + min(widget.winfo_height(), visible_height) - visible_height) / height)

    def _choose_video(self) -> None:
        if self.busy or self._closing:
            return
        selected = filedialog.askopenfilename(title="Choisir une vidéo", filetypes=VIDEO_TYPES)
        if not selected:
            return
        self.video_path = Path(selected)
        self.video_info = None
        self.preview.clear()
        self._hide_gallery()
        self.video_details.set("Lecture des informations de la vidéo…")
        self.status.set("Analyse de la vidéo…")
        self.progress.configure(value=0)
        operation_id = self._begin_operation()
        threading.Thread(target=self._probe_worker, args=(operation_id, self.video_path)).start()

    def _probe_worker(self, operation_id: int, path: Path) -> None:
        try:
            self.gallery.wait_for_worker()
            self.preview.wait_for_worker()
            info = probe_video(path, self.cancel_event)
            self.events.put((operation_id, "probe_ok", info))
        except FrameCutterError as exc:
            self.events.put((operation_id, "error", exc))
        except Exception as exc:  # surface unexpected environment/decoder failures cleanly
            log_unexpected_error("analyse vidéo", exc)
            self.events.put((operation_id, "error", FrameCutterError("Erreur inattendue pendant l’analyse de la vidéo.")))

    def _choose_link(self) -> None:
        if self.busy or self._closing:
            return
        dialog = LinkDialog(self.root, title="Importer un lien vidéo")
        link = dialog.result
        if link is None:
            return
        save_to = getattr(dialog, "save_to", None)
        if save_to is None:
            self.status.set("Téléchargement de l’extrait…" if link.portion else "Téléchargement de la vidéo…")
        else:
            self.status.set("Enregistrement de l’extrait…" if link.portion else "Enregistrement de la vidéo…")
        self.progress.configure(mode="indeterminate", value=0)
        self.progress.start(60)
        operation_id = self._begin_operation()
        if save_to is None:
            worker = threading.Thread(target=self._import_worker, args=(operation_id, link))
        else:
            worker = threading.Thread(target=self._save_video_worker, args=(operation_id, link, save_to))
        worker.start()

    def _import_worker(self, operation_id: int, link: VideoLink) -> None:
        folder = None
        try:
            self.gallery.wait_for_worker()
            self.preview.wait_for_worker()
            folder = create_import_folder()
            path = download_video(link, folder, self.cancel_event)
            self.events.put((operation_id, "import_probe", None))
            try:
                info = probe_video(path, self.cancel_event)
            except OperationCancelled:
                raise
            except FrameCutterError:
                raise FrameCutterError("Le fichier téléchargé ne contient pas de vidéo lisible.") from None
            if link.portion is not None:
                start, end = link.portion
                if abs(info.duration - (end - start)) > 0.25:
                    raise FrameCutterError("L’extrait reçu ne correspond pas aux bornes choisies.")
            self.events.put((operation_id, "import_ok", ImportedSource(path, info, folder, link.portion is not None)))
            folder = None
        except FrameCutterError as exc:
            self.events.put((operation_id, "error", exc))
        except Exception as exc:
            log_unexpected_error("import vidéo", exc)
            self.events.put((operation_id, "error", FrameCutterError("Erreur inattendue pendant l’import vidéo.")))
        finally:
            if folder is not None:
                remove_import_folder(folder)

    def _save_video_worker(self, operation_id: int, link: VideoLink, destination: Path) -> None:
        folder = None
        try:
            self.gallery.wait_for_worker()
            self.preview.wait_for_worker()
            folder = create_import_folder()
            path = download_video(link, folder, self.cancel_event, preserve_audio=True)
            self.events.put((operation_id, "import_probe", None))
            try:
                info = probe_video(path, self.cancel_event)
            except OperationCancelled:
                raise
            except FrameCutterError:
                raise FrameCutterError("Le fichier téléchargé ne contient pas de vidéo lisible.") from None
            if link.portion is not None:
                start, end = link.portion
                if abs(info.duration - (end - start)) > 0.25:
                    raise FrameCutterError("L’extrait reçu ne correspond pas aux bornes choisies.")
            saved = save_video_copy(path, destination, self.cancel_event)
            self.events.put((operation_id, "saved", saved))
        except FrameCutterError as exc:
            self.events.put((operation_id, "error", exc))
        except Exception as exc:
            log_unexpected_error("enregistrement vidéo", exc)
            self.events.put((operation_id, "error", FrameCutterError("Erreur inattendue pendant l’enregistrement vidéo.")))
        finally:
            if folder is not None:
                remove_import_folder(folder)

    def _discard_import(self) -> None:
        if self._import_folder is not None:
            remove_import_folder(self._import_folder)
            self._import_folder = None

    def _default_destination(self, source: Path) -> Path:
        if self._import_folder is None:
            return source.parent
        videos = Path.home() / "Videos"
        return videos if videos.is_dir() else Path.home()

    def _set_source(self, path: Path, info: VideoInfo, is_excerpt: bool = False) -> None:
        self.video_path = path
        self.video_info = info
        self.preview.set_source(path, info)
        dimensions = f"{info.width} × {info.height}" if info.width and info.height else "dimensions inconnues"
        source_rate = f" · {info.frame_rate} im./s source" if info.frame_rate else ""
        name = path.name if len(path.name) <= 42 else f"{path.name[:27]}…{path.name[-12:]}"
        self.video_details.set(f"{name}\n{format_duration(info.duration)} · {dimensions}{source_rate}")
        destination = self.destination.get()
        if not destination.strip() or destination == self._suggested_destination:
            self._suggested_destination = str(self._default_destination(path))
            self.destination.set(self._suggested_destination)
        else:
            self._suggested_destination = None
        if is_excerpt:
            self.status.set("Extrait prêt. Les positions sont mesurées depuis le début de l’extrait.")
        else:
            self.status.set("Vidéo prête. Ajustez les réglages puis lancez l’extraction.")
        self._set_busy_controls(False)

    def _choose_destination(self) -> None:
        initial = str(Path(self.destination.get())) if self.destination.get() else None
        selected = filedialog.askdirectory(title="Choisir le dossier parent des résultats", initialdir=initial)
        if selected:
            self._suggested_destination = None
            self.destination.set(selected)

    def _parse_fps(self) -> float | None:
        try:
            value = float(self.fps.get().replace(",", "."))
        except ValueError:
            return None
        return value if 1 <= value <= 60 else None

    def _parse_interval_seconds(self) -> int | None:
        try:
            value = int(self.interval_seconds.get())
        except ValueError:
            return None
        return value if 1 <= value <= 3600 else None

    def _update_sampling_controls(self) -> None:
        interval_mode = self.sampling_mode.get() == INTERVAL_MODE
        self.rate_label.configure(text="Secondes entre les images" if interval_mode else "Images par seconde")
        if interval_mode:
            self.fps_entry.grid_remove()
            self.interval_entry.grid(row=3, column=0, sticky="ew", padx=(0, S))
        else:
            self.interval_entry.grid_remove()
            self.fps_entry.grid(row=3, column=0, sticky="ew", padx=(0, S))

    def _update_estimate(self) -> None:
        self._update_sampling_controls()
        if not self.video_info:
            self.estimate.set("")
            self.start_button.configure(state="disabled")
            return
        interval_seconds = None
        if self.sampling_mode.get() == INTERVAL_MODE:
            interval_seconds = self._parse_interval_seconds()
            if interval_seconds is None:
                self.estimate.set("Entrez un intervalle entre 1 et 3 600 secondes.")
                self.start_button.configure(state="disabled")
                return
        else:
            fps = self._parse_fps()
            if fps is None:
                self.estimate.set("Entrez une cadence entre 1 et 60 images par seconde.")
                self.start_button.configure(state="disabled")
                return
        try:
            portions = self.preview.portions()
        except FrameCutterError as exc:
            self.estimate.set(str(exc))
            self.start_button.configure(state="disabled")
            return
        duration = sum(end - start for start, end in portions)
        if interval_seconds is None:
            count = sum(round((end - start) * fps) for start, end in portions)
        else:
            count = sum(max(1, math.ceil((end - start) / interval_seconds - 1e-12))
                        for start, end in portions)
        count_text = f"{count:,}".replace(",", "\u202f")
        duration_text = format_duration(duration)
        if duration < 60:
            seconds = f"{duration:.3f}".rstrip("0").rstrip(".").replace(".", ",")
            duration_text = f"{seconds} s"
        estimate = f"Estimation : environ {count_text} images · {duration_text}"
        if interval_seconds is not None:
            estimate += f" · toutes les {interval_seconds} s"
        if len(portions) > 1:
            estimate += f" · {len(portions)} passages"
        if count >= LARGE_EXPORT_COUNT:
            estimate += "\nExport volumineux. Vérifiez l’espace libre avant de lancer."
        self.estimate.set(estimate)
        self.start_button.configure(state="normal" if not self.busy else "disabled")

    def _start_extraction(self) -> None:
        if self.busy or self._closing or not self.video_path or not self.video_info:
            return
        interval_seconds = None
        if self.sampling_mode.get() == INTERVAL_MODE:
            interval_seconds = self._parse_interval_seconds()
            if interval_seconds is None:
                messagebox.showerror(APP_NAME, "Entrez un intervalle compris entre 1 et 3 600 secondes.")
                return
            fps = 5.0  # Unused by the interval mode; keeps the existing engine API compatible.
        else:
            fps = self._parse_fps()
            if fps is None:
                messagebox.showerror(APP_NAME, "Entrez une cadence comprise entre 1 et 60 images par seconde.")
                return
        try:
            portions = self.preview.portions()
        except FrameCutterError as exc:
            messagebox.showerror(APP_NAME, str(exc), parent=self.root)
            return
        entered_destination = self.destination.get().strip()
        destination = Path(entered_destination) if entered_destination else self._default_destination(self.video_path)
        if not destination.is_dir():
            messagebox.showerror(APP_NAME, "Le dossier de destination n’existe pas.")
            return
        if self._import_folder is not None and destination.resolve().is_relative_to(
            self._import_folder
        ):
            messagebox.showerror(APP_NAME, "Choisissez un dossier de sortie en dehors du dossier temporaire d’import.",
                                 parent=self.root)
            return
        self.destination.set(str(destination))
        self.result_dir = None
        self.progress.configure(value=0)
        self.status.set("Préparation de l’extraction…")
        operation_id = self._begin_operation()
        self._hide_gallery()
        self.gallery.clear()
        args = (operation_id, self.video_path, destination, fps, self.image_format.get(), self.video_info.duration,
                portions, interval_seconds)
        threading.Thread(target=self._extract_worker, args=args).start()

    def _begin_operation(self) -> int:
        self._operation_id += 1
        self.cancel_event.clear()
        self._set_busy_controls(True)
        return self._operation_id

    def _extract_worker(
        self, operation_id: int, source: Path, destination: Path, fps: float, image_format: str, duration: float,
        portions: tuple[tuple[float, float], ...], interval_seconds: float | None,
    ) -> None:
        try:
            self.gallery.wait_for_worker()
            self.preview.wait_for_worker()
            result = extract_frames(
                source, destination, fps, image_format, duration,
                lambda n: self.events.put((operation_id, "progress", n)), self.cancel_event,
                portions=portions, interval_seconds=interval_seconds,
            )
            self.events.put((operation_id, "done", result))
        except FrameCutterError as exc:
            self.events.put((operation_id, "error", exc))
        except Exception as exc:
            log_unexpected_error("extraction des images", exc)
            self.events.put((operation_id, "error", FrameCutterError("Erreur inattendue pendant l’extraction des images.")))

    def _cancel(self) -> None:
        if self.busy:
            self.cancel_event.set()
            self.cancel_button.configure(state="disabled")
            self.status.set("Annulation en cours…")

    def _close(self) -> None:
        self._closing = True
        self.preview.pause()
        self.gallery.pause()
        if not self.busy and not self.preview.working and not self.gallery.working:
            self._destroy_window()
            return
        self._cancel()
        self.status.set("Arrêt du traitement avant fermeture…")

    def _destroy_window(self) -> None:
        self.preview.clear()
        self._discard_import()
        self.progress.stop()
        if self._poll_job is not None:
            self.root.after_cancel(self._poll_job)
            self._poll_job = None
        self.root.destroy()

    def _poll_events(self) -> None:
        self._poll_job = None
        self.preview.poll()
        try:
            while True:
                operation_id, kind, payload = self.events.get_nowait()
                if operation_id != self._operation_id or not self.busy:
                    if isinstance(payload, ImportedSource):
                        remove_import_folder(payload.folder)
                    continue
                if kind == "progress":
                    if not self.cancel_event.is_set():
                        value = float(payload)
                        self.progress.configure(value=value)
                        self.status.set(f"Extraction en cours… {value:.0f} %")
                    continue
                if kind == "import_probe":
                    if not self.cancel_event.is_set():
                        self.status.set("Analyse de la vidéo téléchargée…")
                    continue
                if self._closing:
                    if isinstance(payload, ImportedSource):
                        remove_import_folder(payload.folder)
                    self.busy = False
                    break
                if kind == "probe_ok":
                    path, info = self.video_path, payload
                    assert path is not None and isinstance(info, VideoInfo)
                    if self._import_folder is not None and not path.resolve().is_relative_to(
                        self._import_folder
                    ):
                        self._discard_import()
                    self._set_source(path, info)
                elif kind == "import_ok":
                    assert isinstance(payload, ImportedSource)
                    if self.cancel_event.is_set():
                        remove_import_folder(payload.folder)
                        self.status.set("Import annulé.")
                        self._set_busy_controls(False)
                        continue
                    self._discard_import()
                    self._import_folder = payload.folder
                    self._hide_gallery()
                    self._set_source(payload.path, payload.info, payload.is_excerpt)
                elif kind == "saved":
                    assert isinstance(payload, Path)
                    self.progress.configure(value=100)
                    self.status.set(f"Vidéo enregistrée : {payload}")
                    self._set_busy_controls(False)
                elif kind == "done":
                    assert isinstance(payload, Path)
                    self.result_dir = payload
                    self.progress.configure(value=100)
                    self.status.set("Export terminé. Les images sont disponibles dans la galerie.")
                    self._set_busy_controls(False)
                    self._show_gallery(reveal=False)
                elif kind == "error":
                    assert isinstance(payload, FrameCutterError)
                    if payload.output_dir is not None:
                        self.result_dir = payload.output_dir
                    if self.video_info is None:
                        self.video_details.set("Aucune vidéo prête.")
                        self._discard_import()
                    self._set_busy_controls(False)
                    safe_message = sanitize_user_error(str(payload))
                    self.status.set(safe_message.splitlines()[0])
                    if not isinstance(payload, OperationCancelled):
                        detail = safe_message
                        if payload.output_dir is not None:
                            detail += (
                                "\n\nLes images déjà extraites ont été conservées. "
                                "Utilisez « Ouvrir le dossier » ou « Voir les images » pour y accéder."
                            )
                        messagebox.showerror(APP_NAME, detail, parent=self.root)
        except queue.Empty:
            pass
        if self._closing and not self.busy and not self.preview.working and not self.gallery.working:
            self._destroy_window()
            return
        self._poll_job = self.root.after(100, self._poll_events)

    def _set_busy_controls(self, busy: bool) -> None:
        self.busy = busy
        if not busy and str(self.progress.cget("mode")) == "indeterminate":
            self.progress.stop()
            self.progress.configure(mode="determinate", value=0)
        has_source = self.video_info is not None
        self.video_button.configure(state="disabled" if busy else "normal",
                                    text="Changer de vidéo…" if has_source else "Ouvrir un fichier vidéo…",
                                    style="TButton" if has_source else "Primary.TButton")
        self.link_button.configure(state="disabled" if busy else "normal")
        self.fps_entry.configure(state="disabled" if busy else "normal")
        self.interval_entry.configure(state="disabled" if busy else "normal")
        self.sampling_mode_combo.configure(state="disabled" if busy else "readonly")
        self.format_combo.configure(state="disabled" if busy else "readonly")
        self.destination_entry.configure(state="disabled" if busy else "normal")
        self.destination_button.configure(state="disabled" if busy else "normal")
        self.cancel_button.configure(state="normal" if busy else "disabled")
        result_state = "normal" if not busy and self.result_dir is not None else "disabled"
        self.open_button.configure(state=result_state)
        self.gallery_button.configure(state=result_state)
        self._update_estimate()
        self._update_media()

    def _update_media(self) -> None:
        enabled = not self.busy and not self._closing
        if enabled and not (self._compact and self._gallery_visible):
            self.preview.resume()
        else:
            self.preview.pause()
        if enabled and self._gallery_visible:
            self.gallery.resume()
        else:
            self.gallery.pause()

    def _show_gallery(self, *, reveal: bool = True) -> None:
        if self.busy or self._closing or self.result_dir is None:
            return
        try:
            self.gallery.load(self.result_dir)
        except OSError as exc:
            detail = format_os_error(exc, "Vérifiez que le dossier existe et est accessible.")
            self.gallery.show_error(f"Impossible d’afficher les images.\n{detail}")
        self._gallery_visible = True
        self._place_workspace_panels()
        if reveal or self._compact:
            self.root.update_idletasks()
            if self._compact:
                self.canvas.yview_moveto(0)
            else:
                self._reveal(self.gallery)

    def _hide_gallery(self) -> None:
        self._gallery_visible = False
        self._place_workspace_panels()
        if self._compact:
            self.canvas.yview_moveto(0)

    def _open_result(self) -> None:
        result_dir = self.result_dir
        if not result_dir or not result_dir.is_dir():
            return
        try:
            subprocess.Popen([_resolve_explorer_executable(), str(result_dir.resolve())])
        except OSError:
            messagebox.showerror(
                APP_NAME,
                "Impossible d’ouvrir le dossier de résultats dans l’Explorateur Windows.",
                parent=self.root,
            )


def _resolve_explorer_executable() -> str:
    return str(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "explorer.exe")


def main() -> None:
    # Unexpected errors go to stderr, already stripped of personal paths by log_unexpected_error.
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    clean_abandoned_imports()
    if os.name == "nt" and getattr(sys, "frozen", False):
        import ctypes

        # The bundled helpers load their own DLLs instead of the frozen Python libraries.
        ctypes.windll.kernel32.SetDllDirectoryW(None)
    root = tk.Tk()
    FrameCutterApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
