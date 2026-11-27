"""The link import form."""

import tkinter as tk
from pathlib import Path
from tkinter import filedialog, simpledialog, ttk

from frame_engine import FrameCutterError, parse_position
from ui_style import BACKGROUND, L, M, S, XS
from video_import import is_youtube_link, parse_video_link, platform_problems


PLATFORM_NAMES = {"youtube": "YouTube", "instagram": "Instagram"}
EXCERPT_HINT = "Laissez les deux champs vides pour importer toute la vidéo."
EXCERPT_DISABLED_HINT = "Disponible seulement pour un lien YouTube."


def source_label(youtube: bool, instagram: bool) -> str:
    if youtube and instagram:
        return "Lien YouTube, Reel Instagram ou fichier vidéo direct (HTTPS)"
    if youtube:
        return "Lien YouTube ou fichier vidéo direct (HTTPS)"
    if instagram:
        return "Lien de Reel Instagram ou fichier vidéo direct (HTTPS)"
    return "Lien direct vers un fichier vidéo (HTTPS)"


def platform_notice(problems: dict[str, str | None]) -> str:
    """Explain which links work when a platform cannot be used. Empty when all of them can."""
    grouped: dict[str, list[str]] = {}
    for platform, reason in problems.items():
        if reason:
            grouped.setdefault(reason, []).append(PLATFORM_NAMES.get(platform, platform))
    if not grouped:
        return ""
    lines = ["Les liens directs vers un fichier MP4, MOV, WebM, MKV ou AVI fonctionnent."]
    for reason, names in grouped.items():
        subject = " et ".join(names)
        lines.append(f"{subject} indisponible{'s' if len(names) > 1 else ''} : {reason}")
    return "\n".join(lines)


class LinkDialog(simpledialog.Dialog):
    def __init__(self, parent, title=None, platforms: dict[str, str | None] | None = None):
        self.platforms = dict(platform_problems() if platforms is None else platforms)
        super().__init__(parent, title)

    def _available(self, platform: str) -> bool:
        return not self.platforms.get(platform)

    def body(self, master):
        self.configure(background=BACKGROUND)
        panel = ttk.Frame(master, padding=L)
        panel.pack(fill="both", expand=True)
        self.source_label = ttk.Label(panel, text=source_label(self._available("youtube"), self._available("instagram")),
                                      style="Control.TLabel")
        self.source_label.pack(anchor="w")
        self.url_text = tk.StringVar(self)
        self.url = ttk.Entry(panel, width=42, textvariable=self.url_text)
        self.save_to: Path | None = None
        self.url.pack(fill="x", pady=(XS, XS))
        self.url_menu = tk.Menu(self.url, tearoff=False)
        self.url_menu.add_command(label="Coller", command=self._paste_url)
        self.url.bind("<Button-3>", self._show_url_context_menu)
        ttk.Button(panel, text="Coller le lien", command=self._paste_url).pack(anchor="e", pady=(0, M))
        self.platform_notice = ttk.Label(panel, text=platform_notice(self.platforms), style="Meta.TLabel",
                                         wraplength=320, justify="left")
        if self.platform_notice.cget("text"):
            self.platform_notice.pack(anchor="w", pady=(0, M))

        # The excerpt only exists for YouTube: hidden when YouTube cannot be used, disabled for other links.
        self.excerpt = ttk.Frame(panel)
        if self._available("youtube"):
            self.excerpt.pack(fill="x")
        ttk.Label(self.excerpt, text="Extrait YouTube (facultatif)", style="Control.TLabel").pack(anchor="w")
        self.excerpt_hint = ttk.Label(self.excerpt, text=EXCERPT_DISABLED_HINT, style="Meta.TLabel", wraplength=280)
        self.excerpt_hint.pack(anchor="w", pady=(XS, S))
        bounds = ttk.Frame(self.excerpt)
        bounds.pack(anchor="w")
        ttk.Label(bounds, text="Début (mm:ss)").grid(row=0, column=0, sticky="w", padx=(0, M))
        ttk.Label(bounds, text="Fin (mm:ss)").grid(row=0, column=1, sticky="w")
        self.start = ttk.Entry(bounds, width=12)
        self.end = ttk.Entry(bounds, width=12)
        self.start.grid(row=1, column=0, padx=(0, M), pady=(XS, 0))
        self.end.grid(row=1, column=1, pady=(XS, 0))
        self.action_hint = ttk.Label(
            panel, style="Meta.TLabel", wraplength=320, justify="left",
            text="« Importer » ouvre la vidéo dans Frame Cutter. « Enregistrer sans ouvrir… » garde seulement "
                 "une copie dans le dossier de votre choix.",
        )
        self.action_hint.pack(anchor="w", pady=(M, 0))
        self.notice = ttk.Label(panel, style="Meta.TLabel", wraplength=280)
        self.url_text.trace_add("write", lambda *_: self._update_excerpt())
        self._update_excerpt()
        return self.url

    def _excerpt_enabled(self) -> bool:
        return self._available("youtube") and is_youtube_link(self.url_text.get())

    def _update_excerpt(self) -> None:
        enabled = self._excerpt_enabled()
        for field in (self.start, self.end):
            field.state(["!disabled"] if enabled else ["disabled"])
        self.excerpt_hint.configure(text=EXCERPT_HINT if enabled else EXCERPT_DISABLED_HINT)

    def buttonbox(self):
        buttons = ttk.Frame(self, style="Canvas.TFrame")
        buttons.pack(pady=(0, S))
        ttk.Button(buttons, text="Importer", style="Primary.TButton", command=self.ok).pack(side="left", padx=S)
        self.save_button = ttk.Button(buttons, text="Enregistrer sans ouvrir…", command=self._save_video)
        self.save_button.pack(side="left", padx=(0, S))
        ttk.Button(buttons, text="Annuler", command=self.cancel).pack(side="left", padx=(0, S))
        self.bind("<Return>", self.ok)
        self.bind("<Escape>", self.cancel)

    def _save_video(self):
        if not self.validate():
            return
        selected = filedialog.askdirectory(title="Choisir le dossier de destination", parent=self)
        if selected:
            self.save_to = Path(selected)
            self.ok()

    def _show_url_context_menu(self, event):
        self.url.focus_set()
        if not self.url.selection_present():
            self.url.icursor(self.url.index(f"@{event.x}"))
        try:
            self.url_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.url_menu.grab_release()
        return "break"

    def _paste_url(self):
        try:
            text = self.url.clipboard_get()
        except tk.TclError:
            return
        if self.url.selection_present():
            start, end = self.url.index("sel.first"), self.url.index("sel.last")
        else:
            start = end = self.url.index("insert")
        self.url.delete(start, end)
        self.url.insert(start, text)
        self.url.icursor(start + len(text))
        self.url.focus_set()

    def validate(self):
        # Bounds typed for a YouTube link are ignored once the link is no longer a YouTube one.
        if self._excerpt_enabled():
            start, end = self.start.get().strip(), self.end.get().strip()
        else:
            start = end = ""
        self.result = None
        try:
            portion = (parse_position(start), parse_position(end)) if start or end else None
            link = parse_video_link(self.url.get(), portion)
        except (ValueError, OverflowError):
            self._show_error("Renseignez les deux bornes, en secondes ou au format mm:ss.")
            return False
        except FrameCutterError as exc:
            self._show_error(str(exc))
            return False
        reason = self.platforms.get(link.platform) if link.platform else None
        if reason:
            self._show_error(f"{PLATFORM_NAMES.get(link.platform, link.platform)} est indisponible. {reason}")
            return False
        self.result = link
        return True

    def _show_error(self, text):
        self.notice.configure(text=text)
        self.notice.pack(anchor="w", pady=(S, 0))
