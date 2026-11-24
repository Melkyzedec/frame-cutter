"""Browse and remove exported frames within the main window."""

from __future__ import annotations

import os
import queue
import re
import stat
import threading
import tkinter as tk
from pathlib import Path
from tkinter import ttk
from typing import Callable

from frame_engine import FrameCutterError, OperationCancelled, format_os_error, read_preview_frame
from ui_style import ACCENT, BACKGROUND, BODY, INK, L, M, S, XS, step_heading


FRAME_NAME = re.compile(r"frame_([0-9]{6,})\.(jpg|png)")


class ExportGallery(ttk.Frame):
    def __init__(self, parent: ttk.Frame, on_hide: Callable[[], None]) -> None:
        super().__init__(parent, padding=L)
        self._directory: Path | None = None
        self._directory_stat: os.stat_result | None = None
        self._paths: list[Path] = []
        self._active = False
        self._index: int | None = None
        self._request = 0
        self._pending: tuple[int, Path] | None = None
        self._worker: threading.Thread | None = None
        self._poll_job: str | None = None
        self._resize_job: str | None = None
        self._render_size: tuple[int, int] | None = None
        self._cancel = threading.Event()
        self._results: queue.Queue[tuple[int, bytes | str]] = queue.Queue()
        self._photo: tk.PhotoImage | None = None
        self._confirmation: tuple[Path, os.stat_result] | None = None
        self._caption = tk.StringVar(self)
        self._question = tk.StringVar(self)
        self._compact_gallery: bool | None = None
        self._build_ui(on_hide)
        self.clear()

    def _build_ui(self, on_hide: Callable[[], None]) -> None:
        header = ttk.Frame(self)
        header.pack(fill="x")
        step_heading(header, "04", "Images exportées").pack(side="left")
        ttk.Button(header, text="Masquer", style="Quiet.TButton", command=on_hide).pack(side="right")
        self.caption_label = ttk.Label(self, textvariable=self._caption, style="Meta.TLabel")
        self.caption_label.pack(anchor="w", pady=(XS, 0))
        self.selector = ttk.Combobox(self, state="disabled", width=14)
        self.selector.bind("<<ComboboxSelected>>", lambda _: self._select(self.selector.current()))

        self.content = ttk.Frame(self)
        self.content.pack(fill="x", pady=(M, S))
        sidebar = ttk.Frame(self.content)
        self.sidebar = sidebar
        sidebar.pack(side="left", fill="y", padx=(0, S))
        scroll = ttk.Scrollbar(sidebar, orient="vertical")
        scroll.pack(side="right", fill="y")
        self.frames = tk.Listbox(sidebar, width=12, height=6, exportselection=False, yscrollcommand=scroll.set,
                                 background=BACKGROUND, foreground=INK, font=BODY, borderwidth=0,
                                 highlightthickness=1, highlightbackground=BACKGROUND, highlightcolor=ACCENT,
                                 selectbackground=ACCENT, selectforeground=BACKGROUND, selectborderwidth=0)
        self.frames.pack(side="left", fill="both", expand=True)
        scroll.configure(command=self.frames.yview)
        self.frames.bind("<<ListboxSelect>>", self._pick_frame)
        self.frames.bind("<Left>", lambda _: self._move(-1))
        self.frames.bind("<Right>", lambda _: self._move(1))

        screen = tk.Frame(self.content, width=176, height=180, background=BACKGROUND)
        self.screen = screen
        screen.pack(side="left", fill="both", expand=True)
        screen.pack_propagate(False)
        self.picture = tk.Label(screen, background=BACKGROUND, foreground=INK, font=BODY, wraplength=160)
        self.picture.pack(fill="both", expand=True)
        self.picture.bind("<Configure>", self._resized)

        controls = ttk.Frame(self)
        controls.pack(fill="x")
        self.previous_button = ttk.Button(controls, text="Précédente", style="Compact.TButton",
                                          command=lambda: self._move(-1))
        self.previous_button.pack(side="left")
        self.next_button = ttk.Button(controls, text="Suivante", style="Compact.TButton", command=lambda: self._move(1))
        self.next_button.pack(side="left", padx=(S, 0))
        self.delete_button = ttk.Button(controls, text="Supprimer…", style="Quiet.TButton", command=self._delete_selected)
        self.delete_button.pack(side="right")

        self._confirm_bar = ttk.Frame(self)
        ttk.Label(self._confirm_bar, textvariable=self._question, style="Meta.TLabel", wraplength=288).pack(anchor="w")
        self.confirm_delete_button = ttk.Button(self._confirm_bar, text="Supprimer définitivement", command=self._confirm_delete)
        self.confirm_delete_button.pack(side="right", pady=(XS, 0))
        self.cancel_delete_button = ttk.Button(self._confirm_bar, text="Annuler", command=self._cancel_delete)
        self.cancel_delete_button.pack(side="right", padx=(0, S), pady=(XS, 0))
        self._notice = ttk.Label(self, style="Meta.TLabel", wraplength=288)
        self.bind("<Configure>", self._layout_gallery)

    def _layout_gallery(self, event: tk.Event) -> None:
        compact = event.width < 492
        if compact != self._compact_gallery:
            self._compact_gallery = compact
            if compact:
                self.sidebar.pack_forget()
                self.caption_label.pack_forget()
                self.selector.pack(fill="x", pady=(S, 0), before=self.content)
            else:
                self.selector.pack_forget()
                self.sidebar.pack(side="left", fill="y", padx=(0, S), before=self.screen)
                self.caption_label.pack(anchor="w", pady=(XS, 0), before=self.content)
        width = event.width - 2 * L - (0 if compact else self.sidebar.winfo_reqwidth() + S)
        height = max(120, min(440, width * 9 // 16))
        if int(self.screen.cget("height")) != height:
            self.screen.configure(height=height)

    @property
    def working(self) -> bool:
        return self._worker is not None and self._worker.is_alive()

    def wait_for_worker(self) -> None:
        if self._worker is not None:
            self._worker.join()

    def load(self, directory: Path) -> None:
        directory = directory.resolve(strict=True)
        if directory == self._directory:
            return
        directory_stat = directory.stat()
        if not stat.S_ISDIR(directory_stat.st_mode):
            raise OSError("Le dossier de résultat est indisponible.")
        with os.scandir(directory) as entries:
            frames = []
            for entry in entries:
                match = FRAME_NAME.fullmatch(entry.name)
                if match and entry.is_file(follow_symlinks=False):
                    frames.append((int(match[1]), entry.name))
        self.clear()
        self._directory = directory
        self._directory_stat = directory_stat
        self._paths = [directory / name for _, name in sorted(frames)]
        if self._paths:
            self.frames.insert(tk.END, *(f"Image {int(path.stem[6:]):03d}" for path in self._paths))
            self.selector.configure(values=tuple(path.name for path in self._paths))
        self._select(0)

    def clear(self) -> None:
        self.pause()
        self._directory = None
        self._directory_stat = None
        self._paths = []
        self._index = None
        self._photo = None
        self._render_size = None
        self.frames.delete(0, tk.END)
        self.selector.configure(values=())
        self.selector.set("")
        self._caption.set("")
        self._notice.pack_forget()
        self.picture.configure(image="", text="Aucun export à afficher.")
        self._update_buttons()

    def show_error(self, message: str) -> None:
        self.clear()
        self.picture.configure(text=message)

    def pause(self) -> None:
        self._active = False
        self._request += 1
        self._pending = None
        self._cancel.set()
        self._cancel_resize()
        self._dismiss_confirmation()
        self._update_buttons()

    def resume(self) -> None:
        if self._active:
            return
        self._active = True
        if self._index is not None and self._photo is None:
            self._queue_image()
        self._update_buttons()

    def _pick_frame(self, _event: tk.Event) -> None:
        indices = self.frames.curselection()
        if indices and self._active:
            self._select(indices[0])

    def _move(self, offset: int) -> None:
        if self._active and self._index is not None:
            self._select(self._index + offset)

    def _select(self, index: int) -> None:
        if not self._paths:
            self._index = None
            self._photo = None
            self.selector.set("")
            self.picture.configure(image="", text="Aucune image dans cet export.")
            self._caption.set("0 image")
            self._update_buttons()
            return
        if not 0 <= index < len(self._paths) or index == self._index:
            return
        self._dismiss_confirmation()
        self._notice.pack_forget()
        self._cancel_resize()
        self._index = index
        self.frames.selection_clear(0, tk.END)
        self.frames.selection_set(index)
        self.frames.activate(index)
        self.frames.see(index)
        self.selector.current(index)
        self._caption.set(f"{index + 1} / {len(self._paths)} · {self._paths[index].name}")
        self._photo = None
        self.picture.configure(image="", text="Chargement de l’image…")
        if self._active:
            self._queue_image()
        self._update_buttons()

    def _queue_image(self) -> None:
        if self._active and self._index is not None:
            self._request += 1
            self._pending = (self._request, self._paths[self._index])
            self._cancel.set()
            self._schedule_poll()

    def _picture_size(self) -> tuple[int, int]:
        width, height = self.picture.winfo_width(), self.picture.winfo_height()
        if width < 2 or height < 2:
            return 560, 350
        return min(width, 1600), min(height, 1000)

    def _cancel_resize(self) -> None:
        if self._resize_job is not None:
            self.after_cancel(self._resize_job)
            self._resize_job = None

    def _resized(self, _event: tk.Event) -> None:
        self._cancel_resize()
        if self._active and self._index is not None and self._confirmation is None:
            self._resize_job = self.after(140, self._resize_image)

    def _resize_image(self) -> None:
        self._resize_job = None
        if self._confirmation is None and self._render_size != self._picture_size():
            self._queue_image()
            self._update_buttons()

    def _schedule_poll(self) -> None:
        if self._poll_job is None:
            self._poll_job = self.after(50, self._poll)

    def _update_buttons(self) -> None:
        index = self._index
        self.previous_button.configure(state="normal" if self._active and index is not None and index > 0 else "disabled")
        self.next_button.configure(state="normal" if self._active and index is not None and index + 1 < len(self._paths) else "disabled")
        ready = self._active and index is not None and self._worker is None and self._pending is None and self._confirmation is None
        self.delete_button.configure(state="normal" if ready else "disabled")
        self.selector.configure(state="readonly" if self._active and index is not None else "disabled")

    @staticmethod
    def _decode(
        results: queue.Queue[tuple[int, bytes | str]], request: int, path: Path,
        cancel: threading.Event, size: tuple[int, int],
    ) -> None:
        try:
            if not stat.S_ISREG(path.lstat().st_mode):
                raise OSError("Fichier image indisponible.")
            image = read_preview_frame(path, 0, *size, cancel)
        except OperationCancelled:
            return
        except (FrameCutterError, OSError):
            results.put((request, "Impossible d’afficher cette image."))
        else:
            results.put((request, image))

    def _poll(self) -> None:
        self._poll_job = None
        if self._worker is not None and not self._worker.is_alive():
            self._worker = None
        try:
            while True:
                request, image = self._results.get_nowait()
                if request != self._request or not self._active:
                    continue
                if isinstance(image, bytes):
                    try:
                        self._photo = tk.PhotoImage(master=self, data=image, format="png")
                    except tk.TclError:
                        image = "Impossible d’afficher cette image."
                    else:
                        self.picture.configure(image=self._photo, text="")
                        continue
                self._photo = None
                self.picture.configure(image="", text=image)
        except queue.Empty:
            pass
        if self._worker is None and self._pending is not None and self._active:
            request, path = self._pending
            self._pending = None
            self._cancel = threading.Event()
            self._render_size = self._picture_size()
            self._worker = threading.Thread(
                target=self._decode, args=(self._results, request, path, self._cancel, self._render_size),
            )
            self._worker.start()
        self._update_buttons()
        if self._worker is not None:
            self._schedule_poll()

    def _image_stat(self, path: Path) -> os.stat_result:
        if self._directory is None or self._directory_stat is None or not os.path.samestat(self._directory_stat, self._directory.lstat()):
            raise OSError("Le dossier d’export a changé.")
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode):
            raise OSError("L’image sélectionnée a été remplacée.")
        return info

    def _delete_selected(self) -> None:
        if self._index is None or self.delete_button.instate(["disabled"]):
            return
        path = self._paths[self._index]
        try:
            self._confirmation = (path, self._image_stat(path))
        except OSError as exc:
            detail = format_os_error(exc, "Vérifiez les droits d’accès au dossier ou à l’image.")
            self._show_notice(f"Suppression impossible. {detail}")
            return
        self._notice.pack_forget()
        self._question.set(f"Supprimer définitivement {path.name} ?")
        self._confirm_bar.pack(fill="x", pady=(S, 0))
        self.cancel_delete_button.focus_set()
        self._update_buttons()

    def _dismiss_confirmation(self) -> None:
        self._confirmation = None
        self._confirm_bar.pack_forget()

    def _cancel_delete(self) -> None:
        self._dismiss_confirmation()
        self._update_buttons()

    def _confirm_delete(self) -> None:
        if not self._active or self._index is None or self._confirmation is None:
            return
        index = self._index
        path, expected = self._confirmation
        self._dismiss_confirmation()
        if path != self._paths[index]:
            self._update_buttons()
            return
        try:
            if not os.path.samestat(expected, self._image_stat(path)):
                raise OSError("L’image sélectionnée a changé depuis la confirmation.")
            path.unlink()
        except OSError as exc:
            detail = format_os_error(exc, "Vérifiez que le fichier n’est pas verrouillé et reste accessible.")
            self._show_notice(f"Suppression impossible. {detail}")
            self._update_buttons()
            return
        del self._paths[index]
        self.frames.delete(index)
        self.selector.configure(values=tuple(path.name for path in self._paths))
        self._index = None
        self._select(min(index, len(self._paths) - 1))

    def _show_notice(self, message: str) -> None:
        self._notice.configure(text=message)
        self._notice.pack(fill="x", pady=(S, 0))

    def destroy(self) -> None:
        self.pause()
        if self._poll_job is not None:
            self.after_cancel(self._poll_job)
            self._poll_job = None
        self._photo = None
        super().destroy()
