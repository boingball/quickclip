"""Tk desktop interface for QuickClip."""
from collections import Counter
from itertools import groupby
import json
import math
import os
from pathlib import Path
from queue import Empty, Queue
import shutil
import subprocess
from tempfile import NamedTemporaryFile
from threading import Event, Thread
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from quickclip import (
    BatchResult, DEFAULT_MONTAGE_WORKERS, DEFAULT_SCORE, GREEN_PLAYER, KillFeedOptions, NAME_TOKEN, OCR_MODES,
    RECORDING_EXTENSIONS, export_clip, folder_recordings, parse_feed_divider, parse_feed_region, prepare_output,
    preview_feed, probe, process_batch, run, scan_kills, write_montage,
)


def clock(seconds):
    minutes, remainder = divmod(seconds, 60)
    hours, minutes = divmod(int(minutes), 60)
    return f"{hours:02}:{minutes:02}:{remainder:05.2f}"


def settings_path():
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
        return base / "QuickClip" / "settings.json"
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "quickclip" / "settings.json"


class Desktop:
    def __init__(self, root):
        self.root = root
        self.sources = []
        self.input_path = None
        self.default_output = None
        self.output = None
        self.clips = []
        self.montage_review = False
        self.montage_scanning = False
        self.scan_stop = None
        self.completed_files = 0
        self.scan_total = 0
        self.feed_results = []
        self.kept = set()
        self.messages = Queue()
        self.status = tk.StringVar(value="Open an OBS recording or folder to get started.")
        self.filename = tk.StringVar(value="No input selected")
        self.output_name = tk.StringVar(value="No output folder selected")
        self.keep_count = tk.StringVar()
        self.score_enabled = tk.BooleanVar()
        self.player = tk.StringVar()
        self.score = tk.StringVar(value=DEFAULT_SCORE)
        self.feed_region = tk.StringVar(value="58,0,42,30")
        self.feed_divider = tk.StringVar(value="50")
        self.fixed_feed_divider = tk.BooleanVar()
        self.feed_mode = tk.StringVar(value=OCR_MODES[0])
        self.any_feed_mention = tk.BooleanVar()
        self.audio_fallback = tk.BooleanVar()

        root.title("QuickClip")
        root.minsize(720, 460)
        body = ttk.Frame(root, padding=16)
        body.pack(fill="both", expand=True)
        file_row = ttk.Frame(body)
        file_row.pack(fill="x")
        self.open_button = ttk.Button(file_row, text="Open recording…", command=self.open)
        self.open_button.pack(side="left")
        self.folder_button = ttk.Button(file_row, text="Open folder…", command=self.open_folder)
        self.folder_button.pack(side="left", padx=(8, 0))
        ttk.Label(file_row, textvariable=self.filename).pack(side="left", padx=10)
        output_row = ttk.Frame(body)
        output_row.pack(fill="x", pady=(8, 0))
        self.output_button = ttk.Button(output_row, text="Output folder…", command=self.choose_output)
        self.output_button.pack(side="left")
        ttk.Label(output_row, textvariable=self.output_name).pack(side="left", padx=10)

        settings = ttk.LabelFrame(body, text="Highlight detection", padding=10)
        settings.pack(fill="x", pady=12)
        settings.columnconfigure(1, weight=1)
        settings.columnconfigure(3, weight=1)
        self.track = ttk.Combobox(settings, state="readonly", values=("Auto", "Off"))
        self.track.current(0)
        self.threshold = ttk.Entry(settings, width=8)
        self.before = ttk.Entry(settings, width=8)
        self.after = ttk.Entry(settings, width=8)
        self.audio_offset = ttk.Entry(settings, width=8)
        self.audio_offset.insert(0, "0")
        self.montage_limit = ttk.Entry(settings, width=8)
        self.montage_workers = ttk.Entry(settings, width=8)
        self.montage_workers.insert(0, str(DEFAULT_MONTAGE_WORKERS))
        for field, value in ((self.threshold, "-18"), (self.before, "10"), (self.after, "6")):
            field.insert(0, value)
        settings_fields = (
            ("Microphone track", self.track, 0, 0),
            ("Mic threshold (dBFS)", self.threshold, 0, 2),
            ("Player name (kill feed)", ttk.Entry(settings, textvariable=self.player), 1, 0),
            ("Seconds before", self.before, 1, 2),
            ("Score text pattern", ttk.Entry(settings, textvariable=self.score), 2, 0),
            ("Seconds after", self.after, 2, 2),
            ("Export audio offset (ms)", self.audio_offset, 4, 0),
            ("Montage file limit (optional)", self.montage_limit, 5, 0),
            ("Montage scan workers", self.montage_workers, 6, 0),
        )
        for label, field, row, column in settings_fields:
            ttk.Label(settings, text=label).grid(row=row, column=column, sticky="w", padx=(0, 8))
            field.grid(row=row, column=column + 1, sticky="ew", padx=(0, 12), pady=3)
        ttk.Checkbutton(settings, text="Detect score popups", variable=self.score_enabled).grid(
            row=3, column=1, sticky="w")
        self.scan_button = ttk.Button(settings, text="Find highlights", command=self.start_scan, state="disabled")
        self.scan_button.grid(row=3, column=3, sticky="e")
        self.batch_button = ttk.Button(settings, text="Scan and export all",
                                       command=lambda: self.start_scan(auto_export=True))
        self.batch_button.grid(row=4, column=3, sticky="e", pady=(4, 0))
        self.montage_button = ttk.Button(settings, text="Find montage clips",
                                         command=lambda: self.start_scan(montage=True))
        self.montage_button.grid(row=5, column=3, sticky="e", pady=(4, 0))
        self.crop_button = ttk.Button(settings, text="Set kill-feed area…", command=self.configure_feed)
        self.crop_button.grid(row=6, column=3, sticky="e", pady=(4, 0))
        ttk.Checkbutton(settings, text="Match name anywhere in feed", variable=self.any_feed_mention).grid(
            row=7, column=0, columnspan=2, sticky="w")
        ttk.Checkbutton(settings, text="Use loud audio if no kill matches", variable=self.audio_fallback).grid(
            row=7, column=2, columnspan=2, sticky="w")

        results = ttk.Frame(body)
        results.pack(fill="both", expand=True)
        self.table = ttk.Treeview(results, columns=("keep", "source", "start", "end", "reasons"),
                                  displaycolumns=("source", "start", "end", "reasons"), show="headings")
        self.table.heading("keep", text="Keep")
        self.table.column("keep", width=58, anchor="center", stretch=False)
        for name, title, width in (("source", "Recording", 250), ("start", "Start", 100),
                                   ("end", "End", 100), ("reasons", "Detected by", 210)):
            self.table.heading(name, text=title)
            self.table.column(name, width=width, stretch=name in ("source", "reasons"))
        scrollbar = ttk.Scrollbar(results, orient="vertical", command=self.table.yview)
        self.table.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.table.pack(side="left", fill="both", expand=True)
        self.table.bind("<<TreeviewSelect>>", lambda _: self.buttons())
        self.table.bind("<Button-1>", self.click_keep)
        self.table.bind("<Double-1>", self.double_click)
        self.table.bind("<space>", self.toggle_focused)

        actions = ttk.Frame(body)
        actions.pack(fill="x", pady=(10, 0))
        self.preview_button = ttk.Button(actions, text="Play clip", command=self.preview)
        self.preview_button.pack(side="left")
        self.feed_button = ttk.Button(actions, text="OCR readout", command=self.show_feed_results)
        self.feed_button.pack(side="left", padx=(6, 0))
        ttk.Label(actions, textvariable=self.keep_count).pack(side="left", padx=10)
        self.export_button = ttk.Button(actions, text="Export selected…", command=self.export)
        self.export_button.pack(side="right")
        self.build_button = ttk.Button(actions, text="Build montage", command=self.build_montage)
        ttk.Label(body, textvariable=self.status, wraplength=680).pack(fill="x", pady=(10, 0))

        self.busy = False
        self.settings_file = settings_path()
        self.settings_warning_shown = False
        root.protocol("WM_DELETE_WINDOW", self.close)
        self.load_settings()
        self.buttons()
        self.poll_task = root.after(100, self.poll)

    def settings_fields(self):
        return {
            "threshold": self.threshold,
            "before": self.before,
            "after": self.after,
            "audio_offset": self.audio_offset,
            "montage_limit": self.montage_limit,
            "montage_workers": self.montage_workers,
        }

    def load_settings(self):
        try:
            values = json.loads(self.settings_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(values, dict):
            return
        for key, field in self.settings_fields().items():
            if isinstance(values.get(key), str):
                field.delete(0, "end")
                field.insert(0, values[key])
        for key, field in (("player", self.player), ("score", self.score), ("feed_region", self.feed_region),
                           ("feed_divider", self.feed_divider)):
            if isinstance(values.get(key), str):
                field.set(values[key])
        self.score_enabled.set(values.get("score_enabled") is True)
        self.any_feed_mention.set(values.get("any_feed_mention") is True)
        self.audio_fallback.set(values.get("audio_fallback") is True)
        self.fixed_feed_divider.set(values.get("fixed_feed_divider") is True)
        if values.get("feed_mode") in OCR_MODES:
            self.feed_mode.set(values["feed_mode"])
        if isinstance(values.get("output"), str) and values["output"]:
            self.output = Path(values["output"])
            self.show_output()
        if isinstance(values.get("input"), str) and values["input"]:
            self.restore_input(Path(values["input"]), values.get("track"))

    def save_settings(self):
        values = {key: field.get() for key, field in self.settings_fields().items()}
        values.update(input=str(self.input_path) if self.input_path else "",
                      output=str(self.output) if self.output else "",
                      track=self.track.get(), player=self.player.get(), score=self.score.get(),
                      score_enabled=self.score_enabled.get(), feed_region=self.feed_region.get(),
                      any_feed_mention=self.any_feed_mention.get(), audio_fallback=self.audio_fallback.get(),
                      feed_mode=self.feed_mode.get(), feed_divider=self.feed_divider.get(),
                      fixed_feed_divider=self.fixed_feed_divider.get())
        temp = None
        try:
            self.settings_file.parent.mkdir(parents=True, exist_ok=True)
            with NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.settings_file.parent,
                                    prefix=".settings-", delete=False) as stream:
                temp = Path(stream.name)
                json.dump(values, stream, ensure_ascii=False)
            temp.replace(self.settings_file)
        except OSError as error:
            if temp is not None:
                try:
                    temp.unlink(missing_ok=True)
                except OSError:
                    pass
            if not self.settings_warning_shown:
                self.settings_warning_shown = True
                messagebox.showwarning("QuickClip", f"Could not save settings. {error}")

    def close(self):
        if self.scan_stop is not None:
            self.scan_stop.set()
        self.root.after_cancel(self.poll_task)
        self.save_settings()
        self.root.destroy()

    def buttons(self):
        ready = bool(self.table.selection()) and not self.busy
        can_preview = bool(self.table.selection()) and (not self.busy or self.montage_scanning)
        for button in (self.open_button, self.folder_button, self.output_button):
            button.configure(state="disabled" if self.busy else "normal")
        for button in (self.scan_button, self.batch_button, self.montage_button, self.crop_button):
            button.configure(state="normal" if self.sources and not self.busy else "disabled")
        self.feed_button.configure(state="normal" if self.feed_results else "disabled")
        self.export_button.configure(state="normal" if ready else "disabled")
        self.build_button.configure(state="normal" if self.kept and not self.busy else "disabled")
        self.preview_button.configure(state="normal" if can_preview and shutil.which("ffplay") else "disabled")

    def work(self, operation, complete):
        self.busy = True
        self.buttons()

        def worker():
            try:
                self.messages.put(("done", (complete, operation())))
            except Exception as error:
                self.messages.put(("error", str(error)))

        Thread(target=worker, daemon=True).start()

    def poll(self):
        try:
            while True:
                kind, value = self.messages.get_nowait()
                if kind == "progress":
                    self.status.set(value)
                    continue
                if kind == "montage_result":
                    self.completed_files += 1
                    self.feed_results.append(value)
                    self.append_candidates(value)
                    self.update_keep_count()
                    self.buttons()
                    continue
                self.busy = False
                if self.montage_scanning:
                    self.montage_scanning = False
                    self.scan_stop = None
                    self.update_keep_count()
                if kind == "error":
                    self.status.set("Operation failed.")
                    messagebox.showerror("QuickClip", value)
                else:
                    complete, result = value
                    complete(result)
                self.buttons()
        except Empty:
            pass
        self.poll_task = self.root.after(100, self.poll)

    def clear(self, montage=False):
        self.clips = []
        self.feed_results.clear()
        self.kept.clear()
        self.montage_review = montage
        self.keep_count.set("")
        self.table.delete(*self.table.get_children())
        if montage:
            self.table.configure(displaycolumns=("keep", "source", "start", "end", "reasons"))
            self.export_button.pack_forget()
            self.build_button.pack(side="right")
        else:
            self.table.configure(displaycolumns=("source", "start", "end", "reasons"))
            self.build_button.pack_forget()
            self.export_button.pack(side="right")

    def click_keep(self, event):
        if self.montage_review and self.table.identify_column(event.x) == "#1":
            row = self.table.identify_row(event.y)
            if row:
                self.table.focus(row)
                self.table.selection_set(row)
                self.table.focus_set()
                self.toggle_keep(row)
                return "break"

    def double_click(self, event):
        if self.table.identify_row(event.y):
            if self.montage_review and self.table.identify_column(event.x) == "#1":
                return "break"
            self.preview()

    def toggle_focused(self, _):
        if self.montage_review and (row := self.table.focus()):
            self.toggle_keep(row)
            return "break"

    def toggle_keep(self, row):
        if self.busy and not self.montage_scanning:
            return
        index = int(row)
        if index in self.kept:
            self.kept.remove(index)
        else:
            self.kept.add(index)
        self.table.set(row, "keep", "☑" if index in self.kept else "☐")
        self.update_keep_count()
        self.buttons()

    def update_keep_count(self):
        count = f"{len(self.kept)} of {len(self.clips)} clips kept"
        if self.montage_scanning:
            count = f"{self.completed_files}/{self.scan_total} files scanned · {count}"
        self.keep_count.set(count)

    def open(self):
        filters = " ".join(f"*{ext}" for ext in RECORDING_EXTENSIONS)
        filename = filedialog.askopenfilename(filetypes=[("Recordings", filters), ("All files", "*")])
        if filename:
            self.load_recording(Path(filename))

    def restore_input(self, path, track):
        if self.busy or self.sources:
            return
        if path.is_dir():
            self.load_folder(path, track)
        elif path.is_file():
            self.load_recording(path, track)

    def load_recording(self, source, track=None):
        self.status.set("Reading recording metadata…")
        self.work(lambda: probe(source),
                  lambda info: self.set_input([source], source.name, info, source, track=track))

    def open_folder(self):
        directory = filedialog.askdirectory()
        if not directory:
            return
        self.load_folder(Path(directory))

    def load_folder(self, folder, track=None):
        def inspect():
            sources = folder_recordings(folder)
            for source in sources:
                try:
                    recording = probe(source)
                except (RuntimeError, ValueError):
                    continue
                return sources, recording, source
            raise ValueError("None of the recordings in this folder could be opened.")

        def complete(result):
            sources, recording, sample = result
            self.set_input(sources, f"{folder.name} · {len(sources)} recordings",
                           recording, sample, folder, track=track)

        self.status.set("Finding recordings…")
        self.work(inspect, complete)

    def set_input(self, sources, label, recording, sample, folder=None, *, track=None):
        self.clear()
        self.sources = sources
        self.input_path = folder or sample
        self.filename.set(label)
        self.default_output = folder / "Highlights" if folder else None
        self.show_output()
        tracks = ["Auto", "Off"] + [f"{index}: {title}" for index, title in enumerate(recording.audio, 1)]
        self.track.configure(values=tracks)
        self.track.current(tracks.index(track) if track in tracks else 0)
        self.save_settings()
        if folder:
            self.status.set(f"Audio tracks shown from {sample.name}. "
                            "Auto checks each recording. For a manual choice, use the same track position across files.")
        else:
            self.status.set(f"{clock(recording.duration)} · {len(recording.audio)} audio track(s). "
                            "Auto looks for the isolated mic track. You can also pick it manually.")

    def show_output(self):
        directory = self.output or self.default_output
        self.output_name.set(str(directory) if directory else "No output folder selected")

    def choose_output(self):
        directory = filedialog.askdirectory(mustexist=False)
        if directory:
            self.output = Path(directory)
            self.show_output()
            self.save_settings()
            return self.output
        return None

    def configure_feed(self):
        selected = self.table.selection()
        source, start = self.sources[0], 0
        if selected:
            source, clip = self.clips[int(selected[0])]
            start = (clip.start + clip.end) / 2
        window = tk.Toplevel(self.root)
        window.title("Set kill-feed area")
        frame = ttk.Frame(window, padding=12)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text=source.name).pack(anchor="w")
        tools = ttk.Frame(frame)
        tools.pack(fill="x", pady=6)
        ttk.Label(tools, text="Frame time (seconds)").pack(side="left")
        timestamp = ttk.Entry(tools, width=8)
        timestamp.insert(0, f"{start:.1f}")
        timestamp.pack(side="left", padx=6)
        mode = tk.StringVar(frame, value=self.feed_mode.get())
        canvas = tk.Canvas(frame, width=640, height=360, highlightthickness=0, background="black")
        canvas.pack()
        ttk.Label(frame, text="Crop the whole feed, leaving room for long names on both sides.").pack(anchor="w")
        controls = ttk.Frame(frame)
        controls.pack(fill="x", pady=(8, 0))
        ttk.Label(controls, text="Left, top, width, height (%)").pack(side="left")
        crop = tk.StringVar(frame, value=self.feed_region.get())
        entry = ttk.Entry(controls, textvariable=crop, width=24)
        entry.pack(side="left", padx=6)
        divider = tk.StringVar(frame, value=self.feed_divider.get())
        fixed = tk.BooleanVar(frame, value=self.fixed_feed_divider.get())
        divider_row = ttk.Frame(frame)
        divider_row.pack(fill="x", pady=(6, 0))
        ttk.Checkbutton(divider_row, text="Use fixed divider (% from left)", variable=fixed).pack(side="left")
        divider_entry = ttk.Entry(divider_row, textvariable=divider, width=7)
        divider_entry.pack(side="left", padx=6)
        ttk.Label(divider_row, text="Automatic mode finds the gap separately for each row.").pack(side="left")
        origin = None
        photo = None

        def outline():
            canvas.delete("crop")
            divider_entry.configure(state="normal" if fixed.get() else "disabled")
            if photo is not None:
                try:
                    width, height, left, top = parse_feed_region(crop.get())
                except ValueError:
                    return
                canvas.create_rectangle(left * photo.width(), top * photo.height(),
                                        (left + width) * photo.width(), (top + height) * photo.height(),
                                        outline="#ffe15c", width=2, tags="crop")
                if not fixed.get():
                    return
                try:
                    split = (left + width * parse_feed_divider(divider.get())) * photo.width()
                except ValueError:
                    return
                canvas.create_line(split, top * photo.height(), split, (top + height) * photo.height(),
                                   fill="#53d4ff", width=2, dash=(5, 3), tags="crop")

        def read_frame():
            try:
                seconds = float(timestamp.get())
                if not math.isfinite(seconds) or seconds < 0:
                    raise ValueError
            except ValueError:
                messagebox.showerror("QuickClip", "Enter a nonnegative frame time.", parent=window)
                return
            if self.busy:
                return
            self.status.set("Loading kill-feed preview frame…")

            def complete(data):
                nonlocal photo
                if not window.winfo_exists():
                    return
                if not data:
                    self.status.set("No image at that time. Try an earlier frame.")
                    return
                photo = tk.PhotoImage(master=window, data=data, format="PPM")
                canvas.configure(width=photo.width(), height=photo.height())
                canvas.delete("all")
                canvas.create_image(0, 0, image=photo, anchor="nw")
                outline()
                self.status.set("Kill-feed preview loaded.")

            self.work(lambda: run(["ffmpeg", "-v", "error", "-ss", f"{seconds:.3f}",
                                   "-i", str(source), "-map", "0:V:0", "-frames:v", "1",
                                   "-vf", "scale=760:420:force_original_aspect_ratio=decrease",
                                   "-c:v", "ppm", "-f", "image2pipe", "pipe:1"]), complete)

        def begin(event):
            nonlocal origin
            if photo is not None:
                origin = event.x, event.y

        def drag(event):
            if origin is None:
                return
            left, right = sorted(round(100 * min(max(x, 0), photo.width()) / photo.width(), 1)
                                 for x in (origin[0], event.x))
            top, bottom = sorted(round(100 * min(max(y, 0), photo.height()) / photo.height(), 1)
                                 for y in (origin[1], event.y))
            if right - left >= 1 and bottom - top >= 1:
                crop.set(f"{left:g},{top:g},{right - left:.1f},{bottom - top:.1f}")

        def save():
            try:
                parse_feed_region(crop.get())
                if fixed.get():
                    parse_feed_divider(divider.get())
            except ValueError as error:
                messagebox.showerror("QuickClip", str(error), parent=window)
                return
            self.feed_region.set(crop.get())
            self.feed_divider.set(divider.get())
            self.fixed_feed_divider.set(fixed.get())
            self.feed_mode.set(mode.get())
            self.save_settings()
            window.destroy()

        def test_ocr():
            if self.busy:
                return
            try:
                seconds = float(timestamp.get())
                if not math.isfinite(seconds) or seconds < 0:
                    raise ValueError("Enter a nonnegative frame time.")
                region = parse_feed_region(crop.get())
                split = parse_feed_divider(divider.get()) if fixed.get() else None
            except ValueError as error:
                messagebox.showerror("QuickClip", str(error), parent=window)
                return
            treatment = mode.get()
            self.status.set("Testing kill-feed OCR on one frame…")
            self.work(lambda: preview_feed(source, seconds, region, treatment, split),
                      lambda value: self.show_ocr_test(value, source.name, seconds, treatment))

        crop.trace_add("write", lambda *_: outline())
        divider.trace_add("write", lambda *_: outline())
        fixed.trace_add("write", lambda *_: outline())
        canvas.bind("<Button-1>", begin)
        canvas.bind("<B1-Motion>", drag)
        canvas.bind("<ButtonRelease-1>", drag)
        ttk.Button(tools, text="Load frame", command=read_frame).pack(side="left")
        ttk.Button(tools, text="Test OCR", command=test_ocr).pack(side="right")
        ttk.Combobox(tools, textvariable=mode, values=OCR_MODES, state="readonly",
                     width=13).pack(side="right", padx=6)
        def reset_area():
            crop.set("58,0,42,30")
            divider.set("50")
            fixed.set(False)

        ttk.Button(controls, text="Reset top-right crop", command=reset_area).pack(side="left")
        ttk.Button(controls, text="Save area", command=save).pack(side="right", padx=(8, 0))
        outline()
        read_frame()

    def show_ocr_test(self, value, filename, seconds, treatment):
        image, rows, split = value
        dialog = tk.Toplevel(self.root)
        dialog.title("Kill-feed OCR test")
        frame = ttk.Frame(dialog, padding=12)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text=f"{filename} · {seconds:g}s · {treatment}").pack(anchor="w", pady=(0, 6))
        photo = tk.PhotoImage(master=dialog, data=image, format="PPM").subsample(2)
        canvas = tk.Canvas(frame, width=photo.width(), height=photo.height(), highlightthickness=0)
        canvas.image = photo
        canvas.create_image(0, 0, image=photo, anchor="nw")
        if split is not None:
            canvas.create_line(split / 2, 0, split / 2, photo.height(), fill="#1675a4", width=2, dash=(5, 3))
        else:
            for row in rows:
                if row.divider is not None:
                    top = min(word.top for word in row.words) / 2
                    bottom = max(word.top + word.height for word in row.words) / 2
                    canvas.create_line(row.divider / 2, top, row.divider / 2, bottom,
                                       fill="#1675a4", width=2)
        canvas.pack(anchor="w")
        detail = tk.Text(frame, width=70, height=9, wrap="word")
        detail.pack(fill="both", expand=True, pady=(8, 0))
        if rows:
            for row in rows:
                words = " ".join(
                    f"{word.text} [{word.confidence:.0f}, {row.side(word)}"
                    f"{', green' if word.green else ''}]"
                    for word in row.words)
                detail.insert("end", words + "\n")
            if not any(row.readable for row in rows):
                detail.insert("end", "\nNo confident name-like text on this frame.")
        else:
            detail.insert("end", "Tesseract read no text on this frame.")
        detail.configure(state="disabled")
        self.status.set("Kill-feed OCR test complete.")

    def show_feed_results(self):
        lines = []
        for result in sorted(self.feed_results, key=lambda result: result.source.name.casefold()):
            lines.append(result.source.name)
            lines.append(f"Player seen in {result.feed_mentions} sample(s), "
                         f"{result.feed_matches} matching the selected rule.")
            lines.append(f"Left side · {result.feed_left_mentions} sample(s)   "
                         f"Right side · {result.feed_right_mentions} sample(s)   "
                         f"Side unknown · {result.feed_unknown_mentions} sample(s)")
            if result.feed_green_mentions is not None:
                lines.append(f"Player name marked green in {result.feed_green_mentions} sample(s).")
            names = Counter()
            for row, count in result.feed_rows.items():
                for name in set(NAME_TOKEN.findall(row)):
                    names[name] += count
            if names:
                names_list = ", ".join(name for name, _ in names.most_common(12))
                lines.append(f"Possible names and words · {names_list}")
                lines.extend(f"  {count} × {line}" for line, count in result.feed_rows.most_common(12))
            else:
                lines.append("No confident name-like text. Use Test OCR on a frame with the feed visible.")
            if result.feed_uncertain_rows:
                lines.append(f"Skipped {result.feed_uncertain_rows} low-confidence or non-name OCR row(s).")
            lines.append("")
        self.show_details("Kill-feed OCR readout", "\n".join(lines))

    def start_scan(self, auto_export=False, montage=False):
        player = self.player.get().strip()
        sources = self.sources
        try:
            before, after = float(self.before.get()), float(self.after.get())
            audio_offset = self.read_audio_offset() if auto_export else 0
            selection = self.track.current()
            track = "auto" if selection == 0 else selection - 1
            if montage:
                if not player:
                    raise ValueError("Enter your player name to make a kill montage.")
                limit = self.montage_limit.get().strip()
                if limit:
                    if not limit.isascii() or not limit.isdecimal() or int(limit) < 1:
                        raise ValueError("Enter a positive whole number for the montage file limit.")
                    sources = sources[:int(limit)]
                workers = self.montage_workers.get().strip()
                if not workers.isascii() or not workers.isdecimal() or int(workers) < 1:
                    raise ValueError("Enter a positive whole number for the montage scan workers.")
                workers = int(workers)
                fallback = (track or "auto") if self.audio_fallback.get() else 0
                threshold = float(self.threshold.get()) if fallback else -18
                feed = KillFeedOptions(parse_feed_region(self.feed_region.get()),
                                       self.any_feed_mention.get(), fallback, threshold, self.feed_mode.get(),
                                       parse_feed_divider(self.feed_divider.get())
                                       if self.fixed_feed_divider.get() else None)
            else:
                score = self.score.get().strip() if self.score_enabled.get() else ""
                if self.score_enabled.get() and not score:
                    raise ValueError("Enter a score text pattern.")
                options = (track, float(self.threshold.get()), player, score, before, after)
        except ValueError as error:
            messagebox.showerror("QuickClip", str(error))
            return

        output = None
        if auto_export:
            output = self.output or self.default_output or self.choose_output()
            if output is None:
                return
        self.save_settings()

        if montage:
            self.clear(montage=True)
            self.montage_scanning = True
            self.scan_stop = Event()
            self.completed_files = 0
            self.scan_total = len(sources)
            self.update_keep_count()

        def complete(results):
            if not montage:
                self.clear()
                for result in results:
                    self.append_candidates(result)
            failures = [(result.source, result.errors) for result in results if result.errors]
            status = f"Checked {len(results)} recording(s). Found {len(self.clips)} candidate(s)."
            if montage:
                if self.clips:
                    status += " Play clips and untick any you want to leave out."
                if not failures:
                    if not any(NAME_TOKEN.search(line) for result in results for line in result.feed_rows):
                        status += " No confident name-like text. Use Test OCR in Set kill-feed area."
                    elif any(result.feed_mentions for result in results) and not any(
                            result.feed_matches for result in results):
                        if not feed.any_mention and not any(result.feed_left_mentions for result in results):
                            if any(result.feed_unknown_mentions for result in results):
                                status += " OCR could not establish your side on some rows. Check that the crop includes both names."
                            else:
                                status += " Your name was read only on the victim side. See OCR readout."
                        elif feed.mode == GREEN_PLAYER:
                            status += " No name on the selected side passed the green check. See OCR readout."
                    elif not self.clips:
                        status += " See OCR readout to check the names and crop."
            elif output is not None:
                exported = sum(len(result.exports) for result in results)
                status += f" Exported {exported} to {output}."
            if failures:
                status += f" {len(failures)} recording(s) had errors."
            self.status.set(status)
            if failures:
                self.show_failures(failures)

        def progress(message):
            self.messages.put(("progress", message))

        def operation():
            if montage:
                results = []
                for result in scan_kills(sources, player, before, after, progress,
                                         workers=workers, stop=self.scan_stop, feed=feed):
                    results.append(result)
                    self.messages.put(("montage_result", result))
                return results
            return list(process_batch(sources, options, output, progress, audio_offset_ms=audio_offset))

        self.status.set("Scanning recordings…")
        self.work(operation, complete)

    def append_candidates(self, result):
        first = len(self.clips)
        for clip in result.clips:
            index = len(self.clips)
            self.clips.append((result.source, clip))
            if self.montage_review:
                self.kept.add(index)
            self.table.insert("", "end", iid=str(index), values=(
                "☑" if self.montage_review else "", result.source.name, clock(clip.start),
                clock(clip.end), ", ".join(clip.reasons)))
        if self.montage_review and first == 0 and self.clips:
            self.table.selection_set("0")
            self.table.focus("0")

    def build_montage(self):
        try:
            audio_offset = self.read_audio_offset()
        except ValueError as error:
            messagebox.showerror("QuickClip", str(error))
            return
        directory = self.output or self.default_output or self.choose_output()
        if directory is None:
            return
        self.save_settings()
        clips = [self.clips[index] for index in sorted(self.kept)]
        candidates = [BatchResult(source, [clip for _, clip in group], [], [])
                      for source, group in groupby(clips, key=lambda item: item[0])]

        def operation():
            return write_montage(self.sources, candidates, directory,
                                  lambda msg: self.messages.put(("progress", msg)),
                                  audio_offset_ms=audio_offset)

        def complete(value):
            results, path = value
            failures = [(result.source, result.errors) for result in results if result.errors]
            count = sum(len(result.exports) for result in results)
            self.status.set(f"Saved {count} clip(s) to {path}." if path else "No montage was created.")
            if failures:
                self.show_failures(failures)

        self.status.set("Building montage…")
        self.work(operation, complete)

    def show_failures(self, failures):
        details = "\n\n".join(f"{source.name}\n" + "\n".join(errors) for source, errors in failures)
        self.show_details("Recording errors", details)

    def show_details(self, title, details):
        window = tk.Toplevel(self.root)
        window.title(title)
        detail = tk.Text(window, width=85, height=16, wrap="word")
        scrollbar = ttk.Scrollbar(window, command=detail.yview)
        detail.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        detail.pack(side="left", fill="both", expand=True)
        detail.insert("end", details)
        detail.configure(state="disabled")

    def preview(self):
        selected = self.table.selection()
        player = shutil.which("ffplay")
        if (self.busy and not self.montage_scanning) or not selected or not player:
            return
        source, clip = self.clips[int(selected[0])]
        try:
            subprocess.Popen([player, "-loglevel", "error", "-autoexit",
                              "-ss", f"{clip.start:.3f}", "-t", f"{clip.end - clip.start:.3f}",
                              str(source)])
        except OSError as error:
            messagebox.showerror("QuickClip", str(error))

    def read_audio_offset(self):
        try:
            return int(self.audio_offset.get())
        except ValueError as error:
            raise ValueError("Enter the export audio offset in whole milliseconds. Negative brings audio earlier.") from error

    def export(self):
        try:
            audio_offset = self.read_audio_offset()
        except ValueError as error:
            messagebox.showerror("QuickClip", str(error))
            return
        clips = [self.clips[int(index)] for index in self.table.selection()]
        directory = self.output or self.default_output or self.choose_output()
        if directory is None:
            return
        self.save_settings()

        def operation():
            if self.default_output is not None:
                prepare_output(self.sources, directory)
            for index, (source, clip) in enumerate(clips, 1):
                self.messages.put(("progress", f"Exporting clip {index} of {len(clips)}…"))
                export_clip(source, clip, directory,
                            progress=lambda msg: self.messages.put(("progress", msg)),
                            audio_offset_ms=audio_offset)
            return len(clips)

        self.work(operation, lambda count: self.status.set(f"Exported {count} clip(s) to {directory}"))


def launch():
    root = tk.Tk()
    Desktop(root)
    root.mainloop()
