"""Seekable still preview for the Tkinter window."""

from __future__ import annotations

import queue
import threading
import tkinter as tk
from fractions import Fraction
from pathlib import Path
from tkinter import ttk
from typing import Callable

from frame_engine import (
    FrameCutterError, OperationCancelled, VideoInfo, parse_position, read_preview_frame, validate_portion, validate_portions,
)
from ui_style import ACCENT, BACKGROUND, BODY, INK, L, M, S, TIMECODE, XS, step_heading


PREVIEW_WIDTH = 288
PREVIEW_HEIGHT = 162


def _timestamp(seconds: float, precision: int = 1) -> str:
    scale = 10 ** precision
    whole, fraction = divmod(round(seconds * scale), scale)
    minutes, secs = divmod(whole, 60)
    hours, minutes = divmod(minutes, 60)
    time = f"{minutes:02d}:{secs:02d},{fraction:0{precision}d}"
    if precision > 1:
        time = time.rstrip("0").rstrip(",")
    return f"{hours:d}:{time}" if hours else time


class VideoPreview(ttk.Frame):
    def __init__(self, parent: ttk.Frame, on_portion_change: Callable[[], None]) -> None:
        super().__init__(parent, padding=L)
        self._path: Path | None = None
        self._duration = 0.0
        self._end = 0.0
        self._photo: tk.PhotoImage | None = None
        self._displayed: float | None = None
        self._active = False
        self._portions: tuple[tuple[float, float], ...] = ()
        self._on_portion_change = on_portion_change
        self._worker: threading.Thread | None = None
        self._cancel_event = threading.Event()
        self._request = 0
        self._job: str | None = None
        self._results: queue.Queue[tuple[int, float, bytes | str]] = queue.Queue()
        self._position = tk.DoubleVar(self, value=0.0)
        self._time = tk.StringVar(self, value="")
        self.start = tk.StringVar(self)
        self.end = tk.StringVar(self)

        self._size = (PREVIEW_WIDTH, PREVIEW_HEIGHT)
        step_heading(self, "02", "Choisir les passages").pack(anchor="w", pady=(0, M))
        screen = tk.Frame(self, width=PREVIEW_WIDTH, height=PREVIEW_HEIGHT, background=BACKGROUND)
        self._screen = screen
        screen.pack(fill="x")
        screen.pack_propagate(False)
        self.picture = tk.Label(
            screen, text="Choisissez une vidéo pour voir l’aperçu.", background=BACKGROUND,
            foreground=INK, font=BODY, wraplength=PREVIEW_WIDTH - L,
        )
        self.picture.pack(fill="both", expand=True)
        time_row = ttk.Frame(self)
        time_row.pack(fill="x", pady=(M, 0))
        ttk.Label(time_row, text="Position vidéo", style="Control.TLabel").pack(side="left")
        ttk.Label(time_row, textvariable=self._time, style="Timecode.TLabel").pack(side="right")
        self.slider = ttk.Scale(self, from_=0, to=1, variable=self._position, command=self._seek,
                                state="disabled", style="Scrub.Horizontal.TScale")
        self.slider.pack(fill="x", pady=(XS, 0))

        range_row = ttk.Frame(self)
        range_row.pack(anchor="w", pady=(S, XS))
        self._range_label = ttk.Label(range_row, text="Plage à extraire", style="Control.TLabel")
        self._range_label.pack(side="left")
        self.reset_button = ttk.Button(range_row, text="Vidéo entière", style="Quiet.TButton", command=self._reset_portion)
        self.reset_button.pack(side="left", padx=(L, 0))

        portion = ttk.Frame(self)
        portion.pack(anchor="w")
        self._portion_controls: list[ttk.Widget] = [self.reset_button]
        self.use_start_button = ttk.Button(portion, text="Marquer le début", style="Compact.TButton",
                                           command=lambda: self._use_position(self.start))
        self.use_end_button = ttk.Button(portion, text="Marquer la fin", style="Compact.TButton",
                                         command=lambda: self._use_position(self.end))
        for row, (label, variable, button) in enumerate((
            ("Début", self.start, self.use_start_button), ("Fin", self.end, self.use_end_button),
        )):
            ttk.Label(portion, text=label).grid(row=row, column=0, sticky="w", pady=(0, XS))
            entry = ttk.Entry(portion, textvariable=variable, width=12, font=TIMECODE)
            entry.grid(row=row, column=1, sticky="ew", padx=S, pady=(0, XS))
            button.grid(row=row, column=2, sticky="ew", pady=(0, XS))
            self._portion_controls.extend((entry, button))
        self.add_button = ttk.Button(self, text="Ajouter ce passage", command=self._add_portion)
        self.add_button.pack(anchor="w", pady=(S, 0))
        self._portion_controls.append(self.add_button)
        self._portion_notice = ttk.Label(self, style="Meta.TLabel", wraplength=288)

        self._saved = ttk.Frame(self)
        ttk.Label(self._saved, text="Passages à extraire", style="Control.TLabel").pack(anchor="w")
        rows = ttk.Frame(self._saved)
        rows.pack(fill="x", pady=(XS, S))
        scroll = ttk.Scrollbar(rows, orient="vertical")
        scroll.pack(side="right", fill="y")
        self._portion_list = tk.Listbox(rows, width=1, exportselection=False, font=TIMECODE,
                                        background=BACKGROUND, foreground=INK, selectbackground=ACCENT,
                                        selectforeground=BACKGROUND, borderwidth=0, highlightthickness=1,
                                        highlightbackground=BACKGROUND, highlightcolor=ACCENT,
                                        yscrollcommand=scroll.set)
        self._portion_list.pack(side="left", fill="x", expand=True)
        scroll.configure(command=self._portion_list.yview)
        self._portion_list.bind("<<ListboxSelect>>", lambda _: self._update_remove_button())
        self.remove_button = ttk.Button(self._saved, text="Retirer le passage sélectionné", command=self._remove_portion)
        self.remove_button.pack(anchor="w")
        self._update_remove_button()
        for widget in self._portion_controls:
            widget.state(["disabled"])
        self.start.trace_add("write", self._bounds_changed)
        self.end.trace_add("write", self._bounds_changed)
        self.bind("<Configure>", self._resize_preview)

    def _resize_preview(self, event: tk.Event) -> None:
        width = max(160, min(960, event.width - 2 * L))
        size = (width, min(360, width * 9 // 16))
        if size == self._size:
            return
        self._size = size
        self._screen.configure(height=size[1])
        self._displayed = None
        if self._active:
            self._queue_frame(140)

    def portion(self) -> tuple[float, float]:
        try:
            start, end = parse_position(self.start.get()), parse_position(self.end.get())
        except (ValueError, OverflowError) as exc:
            raise FrameCutterError("Saisissez un début et une fin en secondes ou au format mm:ss.") from exc
        return validate_portion(self._duration, start, end)

    def portions(self) -> tuple[tuple[float, float], ...]:
        return self._portions or (self.portion(),)

    def _bounds_changed(self, *_: str) -> None:
        self._portion_notice.pack_forget()
        self._on_portion_change()

    def _add_portion(self) -> None:
        try:
            portion = self.portion()
            self._portions = validate_portions(self._duration, (*self._portions, portion))
        except FrameCutterError as exc:
            self._portion_notice.configure(text=str(exc))
            self._portion_notice.pack(anchor="w", pady=(XS, 0), after=self.add_button)
            return
        self._portion_notice.pack_forget()
        self._show_portions(self._portions.index(portion))
        self._on_portion_change()

    def _remove_portion(self) -> None:
        selection = self._portion_list.curselection()
        if not selection:
            return
        selected, = selection
        self._portions = self._portions[:selected] + self._portions[selected + 1:]
        self._portion_notice.pack_forget()
        self._show_portions(min(selected, len(self._portions) - 1))
        self._on_portion_change()

    def _show_portions(self, selected: int = 0) -> None:
        self._range_label.configure(text="Passage à ajouter" if self._portions else "Plage à extraire")
        self.reset_button.configure(text="Bornes par défaut" if self._portions else "Vidéo entière")
        self._portion_list.delete(0, tk.END)
        if self._portions:
            self._saved.pack(fill="x", pady=(M, 0))
            for start, end in self._portions:
                self._portion_list.insert(tk.END, f"{_timestamp(start, 6)}  →  {_timestamp(end, 6)}")
            self._portion_list.configure(height=min(5, len(self._portions)))
            self._portion_list.selection_set(selected)
            self._portion_list.see(selected)
        else:
            self._saved.pack_forget()
        self._update_remove_button()

    def _update_remove_button(self) -> None:
        enabled = self._active and bool(self._portion_list.curselection())
        self.remove_button.configure(state="normal" if enabled else "disabled")

    def _use_position(self, variable: tk.StringVar) -> None:
        variable.set(_timestamp(self._position.get(), precision=6))

    def _reset_portion(self) -> None:
        self.start.set("00:00")
        self.end.set(_timestamp(self._duration, precision=6))

    @property
    def working(self) -> bool:
        return self._worker is not None and self._worker.is_alive()

    def wait_for_worker(self) -> None:
        # Called by an analysis/export worker before it starts another FFmpeg process.
        if self._worker is not None:
            self._worker.join()

    def _cancel_pending(self) -> None:
        self._request += 1
        self._cancel_event.set()
        if self._job is not None:
            self.after_cancel(self._job)
            self._job = None

    def pause(self) -> None:
        self._active = False
        self._cancel_pending()
        self.slider.state(["disabled"])
        self.slider.configure(cursor="")
        for widget in self._portion_controls:
            widget.state(["disabled"])
        self._update_remove_button()

    def clear(self) -> None:
        self.pause()
        self._path = None
        self._displayed = None
        self._photo = None
        self._portions = ()
        self._show_portions()
        self._position.set(0.0)
        self._time.set("")
        self.start.set("")
        self.end.set("")
        self.picture.configure(image="", text="Choisissez une vidéo pour voir l’aperçu.")

    def set_source(self, path: Path, info: VideoInfo) -> None:
        self.clear()
        self._path = path
        self._duration = info.duration
        self._reset_portion()
        # The timestamp at the duration is past the end of the video stream.
        try:
            frame_interval = float(1 / Fraction(info.frame_rate or "25"))
        except (ValueError, ZeroDivisionError, OverflowError):
            frame_interval = 0.04
        self._end = max(0.0, info.duration - frame_interval)
        self.slider.configure(to=self._end or 1.0)
        self._show_time(0.0)
        self.picture.configure(text="Chargement de l’aperçu…")

    def resume(self) -> None:
        if self._path is None or self._active:
            return
        self._active = True
        if self._end > 0:
            self.slider.state(["!disabled"])
            self.slider.configure(cursor="hand2")
        for widget in self._portion_controls:
            widget.state(["!disabled"])
        self._update_remove_button()
        if self._displayed != self._position.get():
            self._queue_frame(0)

    def _show_time(self, seconds: float) -> None:
        self._time.set(f"{_timestamp(seconds)} / {_timestamp(self._duration)}")

    def _seek(self, value: str) -> None:
        if self.slider.instate(["disabled"]):
            return
        self._show_time(float(value))
        self._queue_frame(140)

    def _queue_frame(self, delay: int) -> None:
        self._cancel_pending()
        self._job = self.after(delay, self._start_frame)

    def _start_frame(self) -> None:
        self._job = None
        if self._path is None:
            return
        if self.working:
            self._job = self.after(30, self._start_frame)
            return
        self._cancel_event = threading.Event()
        self._worker = threading.Thread(
            target=self._decode,
            args=(self._results, self._request, self._path, self._position.get(), self._cancel_event, self._size),
        )
        self._worker.start()

    @staticmethod
    def _decode(
        results: queue.Queue[tuple[int, float, bytes | str]],
        request: int, path: Path, seconds: float, cancel_event: threading.Event, size: tuple[int, int],
    ) -> None:
        try:
            result = read_preview_frame(path, seconds, *size, cancel_event)
        except OperationCancelled:
            return
        except FrameCutterError as exc:
            results.put((request, seconds, str(exc)))
        else:
            results.put((request, seconds, result))

    def poll(self) -> None:
        try:
            while True:
                request, seconds, result = self._results.get_nowait()
                if request != self._request:
                    continue
                self._displayed = seconds
                if isinstance(result, bytes):
                    try:
                        self._photo = tk.PhotoImage(master=self, data=result, format="png")
                    except tk.TclError:
                        result = "Impossible d’afficher cette image."
                    else:
                        self.picture.configure(image=self._photo, text="")
                        continue
                self._photo = None
                self.picture.configure(image="", text=result)
        except queue.Empty:
            pass
