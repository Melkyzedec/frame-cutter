"""Shared visual tokens for the Frame Cutter window."""

import tkinter as tk
from tkinter import ttk

BACKGROUND = "#10151F"
SURFACE = "#192230"
INK = "#F3F6FA"
MUTED = "#A2B2C5"
ACCENT = "#5EE7C4"

XS, S, M, L, XL, XXL = 4, 8, 12, 16, 24, 32

META = ("Segoe UI", -12)
BODY = ("Segoe UI", -14)
HEADING = ("Segoe UI", -20, "bold")
TITLE = ("Segoe UI", -28, "bold")
ACTION = ("Segoe UI", -14, "bold")
TIMECODE = ("Consolas", -14)


def step_heading(parent: ttk.Frame, number: str, title: str) -> ttk.Frame:
    row = ttk.Frame(parent)
    ttk.Label(row, text=number, style="Step.TLabel").pack(side="left", padx=(0, S))
    ttk.Label(row, text=title, style="Heading.TLabel").pack(side="left")
    return row


def _style_scrubber(root: tk.Tk, style: ttk.Style) -> None:
    # Draw only the skin. ttk.Scale still handles pointer and keyboard input.
    rail = tk.PhotoImage(master=root, width=1, height=28)
    rail.put(MUTED, to=(0, 12, 1, 16))
    knobs = []
    for color in (ACCENT, INK, MUTED):
        knob = tk.PhotoImage(master=root, width=28, height=28)
        for y in range(2, 26):
            span = int((12**2 - (y - 13.5)**2)**0.5)
            knob.put(BACKGROUND, to=(14 - span, y, 14 + span, y + 1))
            if abs(y - 13.5) < 10:
                core = int((10**2 - (y - 13.5)**2)**0.5)
                knob.put(color, to=(14 - core, y, 14 + core, y + 1))
        knobs.append(knob)
    # Tk does not keep Python PhotoImage references alive.
    root._scrubber_images = (rail, *knobs)
    style.element_create("Scrub.trough", "image", rail, sticky="ew")
    style.element_create("Scrub.slider", "image", knobs[0], ("disabled", knobs[2]), ("active", knobs[1]))
    style.layout("Scrub.Horizontal.TScale", [
        ("Horizontal.Scale.focus", {"sticky": "nswe", "children": [
            ("Scrub.trough", {"sticky": "nswe", "children": [("Scrub.slider", {"side": "left"})]})
        ]})
    ])


def apply_style(root: tk.Tk) -> None:
    root.configure(background=BACKGROUND)
    style = ttk.Style(root)
    style.theme_use("clam")
    style.configure(".", background=SURFACE, foreground=INK, font=BODY,
                    bordercolor=BACKGROUND, lightcolor=SURFACE, darkcolor=SURFACE,
                    selectbackground=ACCENT, selectforeground=BACKGROUND, focuscolor=ACCENT)
    style.configure("Canvas.TFrame", background=BACKGROUND)
    style.configure("Title.TLabel", background=BACKGROUND, font=TITLE)
    style.configure("Subtitle.TLabel", background=BACKGROUND, foreground=MUTED, font=BODY)
    style.configure("Heading.TLabel", font=HEADING)
    style.configure("Step.TLabel", font=ACTION, background=BACKGROUND, foreground=ACCENT, padding=(S, XS))
    style.configure("Control.TLabel", font=ACTION)
    style.configure("Timecode.TLabel", font=TIMECODE, foreground=INK)
    style.configure("Meta.TLabel", foreground=MUTED, font=META)
    style.configure("Warning.TLabel", background=SURFACE, foreground=INK, font=BODY, padding=(M, S),
                    bordercolor=ACCENT, relief="solid", borderwidth=1)

    style.configure("TButton", font=ACTION, padding=(M, S), background=SURFACE, borderwidth=1,
                    bordercolor=MUTED, relief="solid", lightcolor=MUTED, darkcolor=MUTED, focusthickness=1)
    style.configure("Compact.TButton", padding=(S, S), width=0)
    style.map("TButton", background=[("disabled", BACKGROUND), ("pressed", BACKGROUND)],
              foreground=[("disabled", MUTED), ("active", ACCENT)],
              bordercolor=[("disabled", BACKGROUND), ("focus", ACCENT), ("active", ACCENT)])
    style.configure("Quiet.TButton", bordercolor=SURFACE, lightcolor=SURFACE, darkcolor=SURFACE, padding=(S, S))
    style.configure("Primary.TButton", background=ACCENT, foreground=BACKGROUND,
                    bordercolor=ACCENT, lightcolor=ACCENT, darkcolor=ACCENT,
                    focuscolor=BACKGROUND, padding=(S, S))
    style.map("Primary.TButton", background=[("disabled", BACKGROUND), ("pressed", MUTED), ("active", INK)],
              foreground=[("disabled", MUTED), ("!disabled", BACKGROUND)],
              bordercolor=[("disabled", BACKGROUND), ("focus", INK), ("active", INK)])

    for field in ("TEntry", "TSpinbox", "TCombobox"):
        style.configure(field, fieldbackground=BACKGROUND, foreground=INK, background=BACKGROUND,
                        padding=(S, S), borderwidth=1, arrowsize=L, arrowcolor=INK,
                        bordercolor=BACKGROUND, lightcolor=BACKGROUND, darkcolor=BACKGROUND,
                        insertcolor=INK)
        style.map(field, bordercolor=[("focus", ACCENT)], fieldbackground=[("readonly", BACKGROUND)],
                  foreground=[("disabled", MUTED), ("readonly", INK)])
    style.configure("Horizontal.TProgressbar", background=ACCENT, troughcolor=BACKGROUND,
                    lightcolor=ACCENT, darkcolor=ACCENT, borderwidth=0, thickness=XS)
    _style_scrubber(root, style)
    style.configure("Vertical.TScrollbar", background=MUTED, troughcolor=BACKGROUND,
                    lightcolor=MUTED, darkcolor=MUTED, bordercolor=BACKGROUND, width=S)
    style.map("Vertical.TScrollbar", background=[("disabled", BACKGROUND)],
              lightcolor=[("disabled", BACKGROUND)], darkcolor=[("disabled", BACKGROUND)])
    style.layout("Vertical.TScrollbar", [
        ("Vertical.Scrollbar.trough", {"sticky": "ns", "children": [("Vertical.Scrollbar.thumb", {"sticky": "nswe"})]})
    ])
    root.option_add("*TCombobox*Listbox.background", SURFACE)
    root.option_add("*TCombobox*Listbox.foreground", INK)
    root.option_add("*TCombobox*Listbox.selectBackground", ACCENT)
    root.option_add("*TCombobox*Listbox.selectForeground", BACKGROUND)
    root.option_add("*TCombobox*Listbox.font", BODY)
