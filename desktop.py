"""Tk desktop interface for QuickClip."""
from collections import Counter
from dataclasses import asdict
from itertools import groupby
import json
import math
import os
from pathlib import Path
from queue import Empty, Queue
import re
import shutil
import subprocess
from tempfile import NamedTemporaryFile
from threading import Event, Thread
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from date_picker import pick_dates
from quickclip import (
    BatchResult, DEFAULT_MONTAGE_WORKERS, DEFAULT_SCORE, FEED, GREEN_PLAYER, KillFeedOptions, NAME_TOKEN, OCR_MODES,
    RECORDING_EXTENSIONS, export_clip, folder_recordings, parse_feed_divider, parse_feed_region, prepare_output,
    MontagePaused, SkullReference, VideoAcceleration, badge_scores, kill_badge_match, latest_montage_job,
    make_skull_reference, preview_feed, preview_kill_badge, probe, process_batch, resolve_video_acceleration,
    resume_montage_job, run, save_montage_job, scan_kills, skull_search_area, skull_search_preview,
    WeaponReference, clip_preview_frames, filter_recording_dates, make_weapon_reference, recognize_weapon,
    recording_date, MontageStyle, TRANSITIONS, VERTICAL_MODES, matches_weapons, parse_weapon_filter, settings_path,
    load_presets, merge_weapon_references,
)
from scan_review import SavedScan

MONTAGE_CUES = ("Kill feed", "KILL badge", "Both", "Badge + feed details")


def clock(seconds):
    minutes, remainder = divmod(seconds, 60)
    hours, minutes = divmod(int(minutes), 60)
    return f"{hours:02}:{minutes:02}:{remainder:05.2f}"


class FeedPlayback:
    def __init__(self, player, source, seconds):
        self.position = None
        self.process = subprocess.Popen(
            [player, "-loglevel", "info", "-stats", "-autoexit", "-sync", "video",
             "-window_title", f"QuickClip preview · {source.name}", "-ss", f"{seconds:.3f}", str(source)],
            stderr=subprocess.PIPE, encoding="utf-8", errors="replace",
        )
        Thread(target=self.read_position, daemon=True).start()

    def read_position(self):
        start = 0.0
        with self.process.stderr as stream:
            for line in stream:
                if match := re.search(r"Duration: .*?, start: (-?\d+(?:\.\d+)?)", line):
                    start = float(match[1])
                elif match := re.match(r"\s*(-?\d+\.\d+)\s+(?:A-V|M-A|M-V):", line):
                    self.position = max(0.0, float(match[1]) - start)
        self.process.wait()

    def stop(self):
        if self.process.poll() is None:
            try:
                self.process.terminate()
            except ProcessLookupError:
                pass


class Desktop:
    def __init__(self, root):
        self.root = root
        self.sources = []
        self.recording_dates = {}
        self.thumbnail_task = None
        self.thumbnail_version = 0
        self.settings_requested = True
        self.layout_height = 0
        self.audio_lookup = set()
        self.input_path = None
        self.default_output = None
        self.output = None
        self.clips = []
        self.montage_review = False
        self.montage_scanning = False
        self.scan_stop = None
        self.montage_pause = None
        self.close_after_montage = False
        self.reviewing_saved_scan = False
        self.review_warning_shown = False
        self.shutdown_task = None
        self.can_shutdown = os.name == "nt"
        self.completed_files = 0
        self.scan_total = 0
        self.feed_results = []
        self.feed_window = None
        self.feed_playback = None
        self.kept = set()
        self.messages = Queue()
        self.status = tk.StringVar(value="Open an OBS recording or folder to get started.")
        self.filename = tk.StringVar(value="No input selected")
        self.output_name = tk.StringVar(value="No output folder selected")
        self.from_date = tk.StringVar()
        self.to_date = tk.StringVar()
        self.date_range_label = tk.StringVar(value="No recording dates available")
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
        self.montage_cue = tk.StringVar(value=MONTAGE_CUES[0])
        self.radeon_encode = tk.BooleanVar()
        self.radeon_decode = tk.BooleanVar()
        self.shutdown_when_finished = tk.BooleanVar()
        self.slowmo_multikills = tk.BooleanVar()
        self.mic_at_kills = tk.BooleanVar()
        self.skull_reference = None
        self.weapon_references = ()

        root.title("QuickClip")
        root.minsize(720, 460)
        body = ttk.Frame(root, padding=10)
        body.pack(fill="both", expand=True)
        file_row = ttk.Frame(body)
        file_row.pack(fill="x")
        self.open_button = ttk.Button(file_row, text="Open recording…", command=self.open)
        self.open_button.pack(side="left")
        self.folder_button = ttk.Button(file_row, text="Open folder…", command=self.open_folder)
        self.folder_button.pack(side="left", padx=(8, 0))
        self.settings_button = ttk.Button(file_row, text="Hide settings", command=self.toggle_settings)
        self.settings_button.pack(side="right")
        ttk.Label(file_row, textvariable=self.filename).pack(side="left", padx=10)
        output_row = ttk.Frame(body)
        output_row.pack(fill="x", pady=(8, 0))
        self.output_button = ttk.Button(output_row, text="Output folder…", command=self.choose_output)
        self.output_button.pack(side="left")
        ttk.Label(output_row, textvariable=self.output_name).pack(side="left", padx=10)
        date_row = ttk.Frame(body)
        date_row.pack(fill="x", pady=(8, 0))
        self.date_button = ttk.Button(date_row, text="Pick dates…", command=self.choose_dates)
        self.date_button.pack(side="left")
        ttk.Label(date_row, textvariable=self.date_range_label, wraplength=720).pack(side="left", padx=10)

        self.settings = settings = ttk.LabelFrame(body, text="Highlight and export settings", padding=6)
        settings.pack(fill="x", pady=(8, 4))
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
        self.montage_format = ttk.Combobox(settings, state="readonly", values=("MKV", "MP4"), width=7)
        self.montage_format.current(0)
        self.mic_mix_track = ttk.Entry(settings, width=8)
        self.montage_transition = ttk.Combobox(settings, state="readonly", values=TRANSITIONS, width=12)
        self.montage_transition.current(0)
        self.montage_vertical = ttk.Combobox(settings, state="readonly", values=VERTICAL_MODES, width=18)
        self.montage_vertical.current(0)
        self.only_weapons = ttk.Entry(settings, width=18)
        for field, value in ((self.threshold, "-18"), (self.before, "10"), (self.after, "6")):
            field.insert(0, value)
        settings_fields = (
            ("Microphone track", self.track, 0, 0),
            ("Mic threshold (dBFS)", self.threshold, 0, 2),
            ("Player name (kill feed)", ttk.Entry(settings, textvariable=self.player), 1, 0),
            ("Seconds before", self.before, 1, 2),
            ("Score text pattern", ttk.Entry(settings, textvariable=self.score), 2, 0),
            ("Seconds after", self.after, 2, 2),
            ("Montage file limit", self.montage_limit, 3, 0),
            ("Montage scan workers", self.montage_workers, 3, 2),
            ("Export audio offset (ms)", self.audio_offset, 4, 0),
            ("Montage format", self.montage_format, 8, 0),
            ("Mix mic track (optional)", self.mic_mix_track, 8, 2),
            ("Montage transition", self.montage_transition, 9, 0),
            ("Vertical for TikTok", self.montage_vertical, 9, 2),
            ("Only weapons (e.g. Rocket)", self.only_weapons, 11, 0),
        )
        for label, field, row, column in settings_fields:
            ttk.Label(settings, text=label).grid(row=row, column=column, sticky="w", padx=(0, 8))
            field.grid(row=row, column=column + 1,
                       sticky="ew" if row <= 2 and column == 0 else "w", padx=(0, 12), pady=2)
        ttk.Checkbutton(settings, text="Detect score popups", variable=self.score_enabled).grid(
            row=5, column=0, columnspan=2, sticky="w")
        ttk.Checkbutton(settings, text="Match name anywhere in feed", variable=self.any_feed_mention).grid(
            row=5, column=2, columnspan=2, sticky="w")
        ttk.Checkbutton(settings, text="Use loud audio if no kill matches", variable=self.audio_fallback).grid(
            row=6, column=0, columnspan=2, sticky="w")
        ttk.Label(settings, text="Montage cues").grid(row=4, column=2, sticky="w")
        ttk.Combobox(settings, textvariable=self.montage_cue, values=MONTAGE_CUES,
                     state="readonly", width=20).grid(row=4, column=3, sticky="w", padx=(0, 12), pady=2)
        ttk.Checkbutton(settings, text="Radeon hardware encoding", variable=self.radeon_encode).grid(
            row=7, column=0, columnspan=2, sticky="w")
        ttk.Checkbutton(settings, text="Radeon hardware decoding", variable=self.radeon_decode).grid(
            row=6, column=2, columnspan=2, sticky="w")
        self.presets = load_presets()
        if self.presets:
            preset_row = ttk.Frame(settings)
            preset_row.grid(row=11, column=2, columnspan=2, sticky="w")
            ttk.Label(preset_row, text="Game preset").pack(side="left", padx=(0, 8))
            self.preset_choice = ttk.Combobox(preset_row, state="readonly", width=16,
                                              values=[preset["name"] for preset in self.presets.values()])
            self.preset_choice.current(0)
            self.preset_choice.pack(side="left")
            ttk.Button(preset_row, text="Apply", command=self.apply_preset).pack(side="left", padx=6)
        ttk.Checkbutton(settings, text="Slow motion on multi-kills", variable=self.slowmo_multikills).grid(
            row=10, column=0, columnspan=2, sticky="w")
        ttk.Checkbutton(settings, text="Mic only around kills", variable=self.mic_at_kills).grid(
            row=10, column=2, columnspan=2, sticky="w")
        shutdown = ttk.Checkbutton(settings, text="Shut down PC after montage", variable=self.shutdown_when_finished)
        shutdown.grid(row=7, column=2, columnspan=2, sticky="w")
        if not self.can_shutdown:
            shutdown.state(["disabled"])
        self.review_toolbar = toolbar = ttk.Frame(body)
        toolbar.pack(fill="x", pady=(2, 6))
        self.montage_button = ttk.Button(toolbar, text="Find montage clips",
                                         command=lambda: self.start_scan(montage=True))
        self.scan_button = ttk.Button(toolbar, text="Find highlights", command=self.start_scan)
        self.batch_button = ttk.Button(toolbar, text="Scan and export all",
                                       command=lambda: self.start_scan(auto_export=True))
        self.crop_button = ttk.Button(toolbar, text="Set kill-feed area…", command=self.configure_feed)
        self.resume_scan_button = ttk.Button(toolbar, text="Resume replay scan", command=self.resume_saved_scan)
        for button in (self.montage_button, self.scan_button, self.batch_button,
                       self.crop_button, self.resume_scan_button):
            button.pack(side="left", padx=(0, 5))

        review = ttk.PanedWindow(body, orient="horizontal")
        results = ttk.Frame(review)
        results.rowconfigure(0, weight=1)
        results.columnconfigure(0, weight=1)
        self.result_columns = ("source", "start", "end", "audio", "reasons", "details", "awards")
        self.table = ttk.Treeview(results, columns=("keep", *self.result_columns),
                                  displaycolumns=self.result_columns, show="headings")
        self.table.heading("keep", text="Keep")
        self.table.column("keep", width=58, anchor="center", stretch=False)
        for name, title, width in (("source", "Recording", 230), ("start", "Start", 85),
                                   ("end", "End", 85), ("audio", "Audio", 74),
                                   ("reasons", "Detected by", 128), ("details", "Weapon / target", 150),
                                   ("awards", "Awards", 120)):
            self.table.heading(name, text=title)
            self.table.column(name, width=width, stretch=name in ("source", "reasons", "details", "awards"))
        scrollbar = ttk.Scrollbar(results, orient="vertical", command=self.table.yview)
        horizontal = ttk.Scrollbar(results, orient="horizontal", command=self.table.xview)
        self.table.configure(yscrollcommand=scrollbar.set, xscrollcommand=horizontal.set)
        scrollbar.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        self.table.grid(row=0, column=0, sticky="nsew")
        self.table.bind("<<TreeviewSelect>>", self.select_clip)
        self.table.bind("<Button-1>", self.click_keep)
        self.table.bind("<Double-1>", self.double_click)
        self.table.bind("<space>", self.toggle_focused)

        review.add(results, weight=1)
        self.preview_strip = ttk.LabelFrame(review, text="Selected clip", padding=8, width=226)
        self.preview_title = tk.StringVar(value="Select a clip to see its frames")
        ttk.Label(self.preview_strip, textvariable=self.preview_title, wraplength=205).pack(fill="x")
        self.preview_button = ttk.Button(self.preview_strip, text="Play clip", command=self.preview)
        self.preview_button.pack(anchor="w", pady=(6, 6))
        preview_canvas = tk.Canvas(self.preview_strip, highlightthickness=0, width=200, height=135,
                                   yscrollincrement=24,
                                   background=ttk.Style(root).lookup("TFrame", "background"))
        preview_scroll = ttk.Scrollbar(self.preview_strip, orient="vertical", command=preview_canvas.yview)
        preview_canvas.configure(yscrollcommand=preview_scroll.set)
        preview_scroll.pack(side="right", fill="y")
        preview_canvas.pack(side="left", fill="both", expand=True)
        thumbnails = ttk.Frame(preview_canvas)
        preview_window = preview_canvas.create_window((0, 0), window=thumbnails, anchor="nw")
        thumbnails.bind("<Configure>", lambda _: preview_canvas.configure(scrollregion=preview_canvas.bbox("all")))
        preview_canvas.bind("<Configure>", lambda event: preview_canvas.itemconfigure(preview_window, width=event.width))
        self.preview_canvas = preview_canvas
        self.thumbnail_labels = [ttk.Label(thumbnails, compound="top", anchor="center") for _ in range(3)]
        for label in self.thumbnail_labels:
            label.pack(fill="x", pady=(0, 6))
        for widget in (preview_canvas, thumbnails, *self.thumbnail_labels):
            widget.bind("<MouseWheel>", self.scroll_preview)
            widget.bind("<Button-4>", self.scroll_preview)
            widget.bind("<Button-5>", self.scroll_preview)
        review.add(self.preview_strip, weight=0)

        status = ttk.Label(body, textvariable=self.status, wraplength=680)
        status.pack(side="bottom", fill="x", pady=(4, 0))
        body.bind("<Configure>", lambda event: self.resize_review(event, status))
        self.actions = actions = ttk.Frame(body)
        actions.pack(side="bottom", fill="x", pady=(6, 0))
        self.feed_button = ttk.Button(actions, text="OCR readout", command=self.show_feed_results)
        self.feed_button.pack(side="left", padx=(6, 0))
        self.resume_button = ttk.Button(actions, text="Resume montage", command=self.resume_montage)
        self.resume_button.pack(side="left", padx=(6, 0))
        self.export_button = ttk.Button(actions, text="Export selected…", command=self.export)
        self.export_button.pack(side="right")
        self.build_button = ttk.Button(actions, text="Build montage", command=self.build_montage)
        self.pause_button = ttk.Button(actions, text="Pause montage", command=self.pause_montage)
        self.cancel_shutdown_button = ttk.Button(actions, text="Cancel shutdown", command=self.cancel_shutdown)
        self.keep_label = ttk.Label(actions, textvariable=self.keep_count)
        self.keep_label.pack(side="left", padx=10)
        review.pack(fill="both", expand=True)

        self.busy = False
        self.settings_file = settings_path()
        self.review_directory = self.settings_file.parent / "scan-review"
        self.saved_scan = SavedScan.load(self.review_directory)
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
            "mic_mix_track": self.mic_mix_track,
            "only_weapons": self.only_weapons,
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
                           ("feed_divider", self.feed_divider), ("from_date", self.from_date),
                           ("to_date", self.to_date)):
            if isinstance(values.get(key), str):
                field.set(values[key])
        self.score_enabled.set(values.get("score_enabled") is True)
        self.any_feed_mention.set(values.get("any_feed_mention") is True)
        self.audio_fallback.set(values.get("audio_fallback") is True)
        self.fixed_feed_divider.set(values.get("fixed_feed_divider") is True)
        self.radeon_encode.set(values.get("radeon_encode") is True)
        self.radeon_decode.set(values.get("radeon_decode") is True)
        if values.get("feed_mode") in OCR_MODES:
            self.feed_mode.set(values["feed_mode"])
        if values.get("montage_cue") in MONTAGE_CUES:
            self.montage_cue.set(values["montage_cue"])
        if values.get("montage_format") in self.montage_format["values"]:
            self.montage_format.set(values["montage_format"])
        if values.get("montage_transition") in TRANSITIONS:
            self.montage_transition.set(values["montage_transition"])
        if values.get("montage_vertical") in VERTICAL_MODES:
            self.montage_vertical.set(values["montage_vertical"])
        self.slowmo_multikills.set(values.get("slowmo_multikills") is True)
        self.mic_at_kills.set(values.get("mic_at_kills") is True)
        stored_reference = values.get("skull_reference")
        if isinstance(stored_reference, dict):
            try:
                self.skull_reference = SkullReference(tuple(stored_reference["box"]), tuple(stored_reference["pixels"]))
            except (KeyError, TypeError, ValueError):
                pass
        try:
            self.weapon_references = tuple(
                WeaponReference(item["label"], item["mode"], tuple(item["box"]), tuple(item["pixels"]))
                for item in values.get("weapon_references", ())
            )
        except (KeyError, TypeError, ValueError):
            pass
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
                      fixed_feed_divider=self.fixed_feed_divider.get(), montage_cue=self.montage_cue.get(),
                      montage_format=self.montage_format.get(),
                      montage_transition=self.montage_transition.get(), montage_vertical=self.montage_vertical.get(),
                      slowmo_multikills=self.slowmo_multikills.get(), mic_at_kills=self.mic_at_kills.get(),
                      from_date=self.from_date.get(), to_date=self.to_date.get(),
                      radeon_encode=self.radeon_encode.get(), radeon_decode=self.radeon_decode.get())
        if self.skull_reference is not None:
            values["skull_reference"] = asdict(self.skull_reference)
        values["weapon_references"] = [asdict(reference) for reference in self.weapon_references]
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
        if self.montage_pause is not None:
            self.close_after_montage = True
            self.pause_montage()
            return
        if self.montage_scanning and self.scan_stop is not None:
            self.close_after_montage = True
            self.scan_stop.set()
            self.status.set("Saving replay scan and closing…")
            return
        self.cancel_shutdown(quiet=True)
        self.stop_feed_playback()
        if self.thumbnail_task is not None:
            self.root.after_cancel(self.thumbnail_task)
        if self.scan_stop is not None:
            self.scan_stop.set()
        self.root.after_cancel(self.poll_task)
        self.save_settings()
        self.root.destroy()

    def stop_feed_playback(self):
        if self.feed_playback is not None:
            self.feed_playback.stop()
            self.feed_playback = None

    def buttons(self):
        ready = bool(self.table.selection()) and not self.busy
        can_preview = bool(self.table.selection()) and (not self.busy or self.montage_scanning)
        for button in (self.open_button, self.folder_button, self.output_button):
            button.configure(state="disabled" if self.busy else "normal")
        self.date_button.configure(state="normal" if self.recording_dates and not self.busy else "disabled")
        for button in (self.scan_button, self.batch_button, self.montage_button, self.crop_button):
            button.configure(state="normal" if self.sources and not self.busy else "disabled")
        self.feed_button.configure(state="normal" if self.feed_results else "disabled")
        self.export_button.configure(state="normal" if ready else "disabled")
        self.build_button.configure(state="normal" if self.kept and not self.busy else "disabled")
        directory = self.output or self.default_output
        self.resume_button.configure(state="normal" if not self.busy and latest_montage_job(directory) else "disabled")
        saved = self.saved_scan
        has_pending = saved is not None and bool(saved.pending())
        can_restore = saved is not None and self.input_path == saved.input_path and (
            has_pending or not self.reviewing_saved_scan)
        self.resume_scan_button.configure(text="Resume replay scan" if has_pending else "Restore replay review",
                                          state="normal" if can_restore and not self.busy else "disabled")
        if self.montage_pause is not None:
            self.pause_button.pack(side="right", padx=(0, 6), before=self.keep_label)
            self.pause_button.configure(state="disabled" if self.montage_pause.is_set() else "normal")
        else:
            self.pause_button.pack_forget()
        self.preview_button.configure(state="normal" if can_preview and shutil.which("ffplay") else "disabled")

    def work(self, operation, complete):
        self.cancel_shutdown(quiet=True)
        self.busy = True
        self.buttons()

        def worker():
            try:
                self.messages.put(("done", (complete, operation())))
            except MontagePaused:
                self.messages.put(("paused", None))
            except Exception as error:
                self.messages.put(("error", str(error)))

        Thread(target=worker, daemon=True).start()

    def poll(self):
        try:
            while True:
                kind, value = self.messages.get_nowait()
                if kind == "progress":
                    if self.montage_pause is None or not self.montage_pause.is_set():
                        self.status.set(value)
                    continue
                if kind == "montage_result":
                    self.completed_files += 1
                    self.feed_results.append(value)
                    self.append_candidates(value)
                    try:
                        self.saved_scan.save_result(value)
                    except OSError as error:
                        self.warn_review_save(error)
                    self.update_keep_count()
                    self.buttons()
                    continue
                if kind == "thumbnails":
                    self.show_thumbnails(*value)
                    continue
                if kind == "audio_metadata":
                    self.show_audio_metadata(*value)
                    continue
                self.busy = False
                self.montage_pause = None
                if self.montage_scanning:
                    self.montage_scanning = False
                    self.scan_stop = None
                    self.update_keep_count()
                if self.close_after_montage:
                    self.close()
                    return
                if kind == "paused":
                    self.status.set("Montage paused. Completed clips are saved. Click Resume montage to continue.")
                elif kind == "error":
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
        self.reviewing_saved_scan = False
        self.keep_count.set("")
        self.table.delete(*self.table.get_children())
        if montage:
            self.table.configure(displaycolumns=("keep", *self.result_columns))
            self.export_button.pack_forget()
            self.build_button.pack(side="right", before=self.keep_label)
        else:
            self.table.configure(displaycolumns=self.result_columns)
            self.build_button.pack_forget()
            self.export_button.pack(side="right", before=self.keep_label)

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
        if self.reviewing_saved_scan:
            source, clip = self.clips[index]
            try:
                self.saved_scan.update_choice(source, clip, index in self.kept)
            except (KeyError, OSError) as error:
                self.warn_review_save(error)
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
            dates = {source: recording_date(source) for source in sources}
            for source in sources:
                try:
                    recording = probe(source)
                except (RuntimeError, ValueError):
                    continue
                return sources, recording, source, dates
            raise ValueError("None of the recordings in this folder could be opened.")

        def complete(result):
            sources, recording, sample, dates = result
            self.set_input(sources, f"{folder.name} · {len(sources)} recordings",
                           recording, sample, folder, track=track, dates=dates)

        self.status.set("Finding recordings…")
        self.work(inspect, complete)

    def set_input(self, sources, label, recording, sample, folder=None, *, track=None, dates=None):
        self.clear()
        self.sources = sources
        self.recording_dates = dates if dates is not None else {sample: recording_date(sample)}
        self.input_path = folder or sample
        self.filename.set(label)
        self.default_output = folder / "Highlights" if folder else None
        self.show_output()
        self.show_date_range()
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
        if self.saved_scan and self.saved_scan.input_path == self.input_path:
            self.restore_replay_list()

    def show_date_range(self):
        oldest, newest = min(self.recording_dates.values()), max(self.recording_dates.values())
        first, last = self.from_date.get(), self.to_date.get()
        current = f"{first or 'Any start'} to {last or 'Any end'}" if first or last else "All dates"
        self.date_range_label.set(f"{current} · Oldest {oldest} · Newest {newest}")

    def choose_dates(self):
        dates = self.recording_dates

        def apply(first, last):
            filter_recording_dates(dates, first, last)
            self.from_date.set(first)
            self.to_date.set(last)
            self.show_date_range()
            self.save_settings()

        pick_dates(self.root, self.from_date.get(), self.to_date.get(),
                   min(dates.values()), max(dates.values()), apply)

    def toggle_settings(self):
        self.settings_requested = not bool(self.settings.winfo_manager())
        self.show_settings(self.settings_requested)

    def show_settings(self, visible):
        if visible:
            if not self.settings.winfo_manager():
                self.settings.pack(fill="x", pady=(8, 4), before=self.review_toolbar)
            self.settings_button.configure(text="Hide settings")
        else:
            self.settings.pack_forget()
            self.settings_button.configure(text="Show settings")

    def resize_review(self, event, status):
        status.configure(wraplength=max(250, event.width - 24))
        if event.height != self.layout_height:
            self.layout_height = event.height
            self.show_settings(self.settings_requested and event.height >= 645)

    def scroll_preview(self, event):
        direction = -1 if event.num == 4 or (event.num != 5 and event.delta > 0) else 1
        self.preview_canvas.yview_scroll(direction, "units")
        return "break"

    def select_clip(self, _):
        self.buttons()
        self.thumbnail_version += 1
        if self.thumbnail_task is not None:
            self.root.after_cancel(self.thumbnail_task)
            self.thumbnail_task = None
        selected = self.table.selection()
        for label in self.thumbnail_labels:
            label.configure(image="", text="")
            label.image = None
        self.preview_canvas.yview_moveto(0)
        if not selected:
            self.preview_title.set("Select a clip to see its frames")
            return
        source, clip = self.clips[int(selected[0])]
        self.preview_title.set(f"{source.name}\n{clock(clip.start)}–{clock(clip.end)}")
        self.thumbnail_labels[0].configure(text="Loading frames…")
        self.thumbnail_task = self.root.after(150, self.load_thumbnails, self.thumbnail_version, source, clip)

    def load_thumbnails(self, version, source, clip):
        self.thumbnail_task = None

        def worker():
            try:
                frames = clip_preview_frames(source, clip)
            except (OSError, RuntimeError, ValueError):
                frames = []
            self.messages.put(("thumbnails", (version, frames)))

        Thread(target=worker, daemon=True).start()

    def show_thumbnails(self, version, frames):
        if version != self.thumbnail_version:
            return
        if not any(data for _, data in frames):
            self.thumbnail_labels[0].configure(text="Preview frames unavailable")
            return
        for label, (seconds, data) in zip(self.thumbnail_labels, frames):
            if data:
                label.image = tk.PhotoImage(master=self.root, data=data, format="PPM")
                label.configure(image=label.image, text=clock(seconds))

    def show_output(self):
        directory = self.output or self.default_output
        self.output_name.set(str(directory) if directory else "No output folder selected")

    def choose_output(self):
        directory = filedialog.askdirectory(mustexist=False)
        if directory:
            self.output = Path(directory)
            self.show_output()
            self.save_settings()
            self.buttons()
            return self.output
        return None

    def configure_feed(self):
        if self.feed_window is not None:
            self.feed_window.lift()
            return
        selected = self.table.selection()
        source, start = self.sources[0], 0
        if selected:
            source, clip = self.clips[int(selected[0])]
            start = (clip.start + clip.end) / 2
        window = tk.Toplevel(self.root)
        self.feed_window = window
        window.title("Set kill-feed area")
        frame = ttk.Frame(window, padding=12)
        frame.pack(fill="both", expand=True)
        sources = tuple(self.sources)
        recordings = ttk.Frame(frame)
        recordings.pack(fill="x")
        ttk.Label(recordings, text="Recording").pack(side="left")
        recording = ttk.Combobox(recordings, state="readonly", values=[item.name for item in sources])
        recording.current(sources.index(source))
        recording.pack(side="left", fill="x", expand=True, padx=(6, 0))
        tools = ttk.Frame(frame)
        tools.pack(fill="x", pady=6)
        ttk.Label(tools, text="Frame time (seconds)").pack(side="left")
        timestamp = ttk.Entry(tools, width=8)
        timestamp.insert(0, f"{start:.1f}")
        timestamp.pack(side="left", padx=6)
        playback_controls = ttk.Frame(frame)
        playback_controls.pack(fill="x", pady=(0, 6))
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

        def close_area():
            self.stop_feed_playback()
            self.feed_window = None
            window.destroy()

        def frame_time():
            seconds = float(timestamp.get())
            if not math.isfinite(seconds) or seconds < 0:
                raise ValueError
            return seconds

        def set_time(seconds):
            timestamp.delete(0, "end")
            timestamp.insert(0, f"{seconds:.3f}")

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
                seconds = frame_time()
            except ValueError:
                messagebox.showerror("QuickClip", "Enter a nonnegative frame time.", parent=window)
                return
            if self.busy:
                return
            requested_source = source
            self.status.set("Loading kill-feed preview frame…")

            def complete(data):
                nonlocal photo
                if not window.winfo_exists():
                    return
                if requested_source != source:
                    read_frame()
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
                                   "-i", str(requested_source), "-map", "0:V:0", "-frames:v", "1",
                                   "-vf", "scale=760:420:force_original_aspect_ratio=decrease",
                                   "-c:v", "ppm", "-f", "image2pipe", "pipe:1"]), complete)

        def change_recording(_):
            nonlocal source, photo, origin
            self.stop_feed_playback()
            source = sources[recording.current()]
            photo = origin = None
            canvas.delete("all")
            set_time(0)
            use_time.configure(state="disabled")
            read_frame()

        def play_recording():
            player = shutil.which("ffplay")
            if player is None:
                messagebox.showerror("QuickClip", "Install FFplay and add it to PATH to play recordings.",
                                     parent=window)
                return
            try:
                seconds = frame_time()
            except ValueError:
                messagebox.showerror("QuickClip", "Enter a nonnegative frame time.", parent=window)
                return
            self.stop_feed_playback()
            use_time.configure(state="disabled")
            try:
                self.feed_playback = FeedPlayback(player, source, seconds)
            except OSError as error:
                messagebox.showerror("QuickClip", str(error), parent=window)
                return
            use_time.configure(state="normal")

        def use_playback_time():
            position = self.feed_playback.position if self.feed_playback is not None else None
            if position is None:
                messagebox.showinfo("QuickClip", "Play the recording, then pause on the kill before copying the time.",
                                    parent=window)
                return
            set_time(position)
            read_frame()

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
            close_area()

        def test_ocr(badge=False):
            if self.busy:
                return
            try:
                seconds = frame_time()
            except ValueError:
                messagebox.showerror("QuickClip", "Enter a nonnegative frame time.", parent=window)
                return
            requested_source = source
            if badge:
                treatment = "KILL badge"
                def operation():
                    return preview_kill_badge(requested_source, seconds)
            else:
                try:
                    region = parse_feed_region(crop.get())
                    split = parse_feed_divider(divider.get()) if fixed.get() else None
                except ValueError as error:
                    messagebox.showerror("QuickClip", str(error), parent=window)
                    return
                treatment = mode.get()
                def operation():
                    return preview_feed(requested_source, seconds, region, treatment, split)
            self.status.set("Testing KILL badge OCR…" if badge else "Testing kill-feed OCR…")

            def complete(value):
                if not window.winfo_exists():
                    return
                if requested_source == source:
                    self.show_ocr_test(value, requested_source.name, seconds, treatment, badge=badge)
                else:
                    read_frame()

            self.work(operation, complete)

        crop.trace_add("write", lambda *_: outline())
        divider.trace_add("write", lambda *_: outline())
        fixed.trace_add("write", lambda *_: outline())
        canvas.bind("<Button-1>", begin)
        canvas.bind("<B1-Motion>", drag)
        canvas.bind("<ButtonRelease-1>", drag)
        window.protocol("WM_DELETE_WINDOW", close_area)
        recording.bind("<<ComboboxSelected>>", change_recording)
        ttk.Button(playback_controls, text="Play recording", command=play_recording).pack(side="left")
        use_time = ttk.Button(playback_controls, text="Use playback time", command=use_playback_time,
                              state="disabled")
        use_time.pack(side="left", padx=6)
        ttk.Label(playback_controls, text="In the player, use Space to pause and the arrow keys to seek.").pack(side="left")
        ttk.Button(tools, text="Load frame", command=read_frame).pack(side="left")
        ttk.Button(tools, text="Test KILL badge", command=lambda: test_ocr(badge=True)).pack(side="right", padx=(6, 0))
        ttk.Button(tools, text="Test feed OCR", command=test_ocr).pack(side="right")
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

    def show_ocr_test(self, value, filename, seconds, treatment, *, badge=False):
        image, rows, split = value
        dialog = tk.Toplevel(self.root)
        dialog.title("KILL badge OCR test" if badge else "Kill-feed OCR test")
        frame = ttk.Frame(dialog, padding=12)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text=f"{filename} · {seconds:g}s · {treatment}").pack(anchor="w", pady=(0, 6))
        photo = tk.PhotoImage(master=dialog, data=image, format="PPM").subsample(2)
        canvas = tk.Canvas(frame, width=photo.width(), height=photo.height(), highlightthickness=0)
        canvas.image = photo
        canvas.create_image(0, 0, image=photo, anchor="nw")
        scores = badge_scores(rows) if badge else []
        if badge:
            for score in scores:
                canvas.create_rectangle(*(coord / 2 for coord in skull_search_area(score)),
                                        outline="#ffe15c", width=2)
                canvas.create_rectangle(score.left / 2, score.top / 2, score.right / 2,
                                        (score.top + score.height) / 2, outline="#53d4ff", width=2)
        elif split is not None:
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

        def render_details():
            detail.configure(state="normal")
            detail.delete("1.0", "end")
            if badge:
                evidence = kill_badge_match(image, rows, self.skull_reference)
                verdict = "Detected" if evidence else "No match"
                detail.insert("end", f"KILL badge · {verdict} on this frame\n\n")
                if evidence:
                    detail.insert("end", f"Matched by {evidence}.\n\n")
                if scores:
                    score_text = ", ".join(score.text for score in scores)
                    detail.insert("end", f"Score read · {score_text}\nBlue outlines the score. Yellow marks the skull search area.\n\n")
            if rows:
                for row in rows:
                    if badge:
                        words = " ".join(f"{word.text} [{word.confidence:.0f}]" for word in row.words)
                    else:
                        words = " ".join(
                            f"{word.text} [{word.confidence:.0f}, {row.side(word)}"
                            f"{', green' if word.green else ''}]"
                            for word in row.words)
                    weapon = recognize_weapon(image, row, self.weapon_references, treatment) if not badge else None
                    detail.insert("end", words + (f" · Weapon: {weapon}" if weapon else "") + "\n")
                if not badge and not any(row.readable for row in rows):
                    detail.insert("end", "\nNo confident name-like text on this frame.")
            else:
                detail.insert("end", "Tesseract read no text on this frame.")
            detail.configure(state="disabled")

        if scores:
            score = max(scores, key=lambda word: word.confidence)
            self.add_skull_reference_controls(frame, image, score, render_details)
        if not badge:
            self.add_weapon_reference_controls(frame, canvas, image, rows, treatment, render_details)
        detail.pack(fill="both", expand=True, pady=(8, 0))
        render_details()
        self.status.set("KILL badge OCR test complete." if badge else "Kill-feed OCR test complete.")

    def add_weapon_reference_controls(self, frame, canvas, image, rows, mode, render_details):
        controls = ttk.Frame(frame)
        controls.pack(fill="x", pady=(6, 0))
        ttk.Label(controls, text="Weapon label").pack(side="left")
        name = ttk.Entry(controls, width=16)
        name.pack(side="left", padx=6)
        ttk.Label(frame, text="Draw a tight box around an icon between two readable names.").pack(anchor="w")
        start = bounds = None

        def drag(event):
            nonlocal bounds
            if start is None:
                return
            left, right = sorted((start[0], max(0, min(canvas.image.width(), event.x))))
            top, bottom = sorted((start[1], max(0, min(canvas.image.height(), event.y))))
            bounds = (2 * left, 2 * top, 2 * right, 2 * bottom)
            canvas.delete("weapon-selection")
            canvas.create_rectangle(left, top, right, bottom, outline="#ffe15c", width=2,
                                    tags="weapon-selection")
            save_button.configure(state="normal" if left < right and top < bottom else "disabled")

        def begin(event):
            nonlocal start
            start = (event.x, event.y)
            drag(event)

        def save():
            try:
                reference = make_weapon_reference(image, rows, bounds, name.get(), mode)
            except ValueError as error:
                messagebox.showerror("QuickClip", str(error), parent=frame.winfo_toplevel())
                return
            self.weapon_references = tuple(
                item for item in self.weapon_references
                if (item.label.casefold(), item.mode) != (reference.label.casefold(), mode)
            ) + (reference,)
            self.save_settings()
            render_details()

        def clear():
            self.weapon_references = tuple(item for item in self.weapon_references if item.mode != mode)
            self.save_settings()
            render_details()

        canvas.bind("<Button-1>", begin)
        canvas.bind("<B1-Motion>", drag)
        canvas.bind("<ButtonRelease-1>", drag)
        save_button = ttk.Button(controls, text="Save weapon icon", command=save, state="disabled")
        save_button.pack(side="left")
        ttk.Button(controls, text="Clear weapon labels", command=clear).pack(side="left", padx=6)

    def add_skull_reference_controls(self, frame, image, score, render_details):
        crop = skull_search_preview(image, score)
        if crop is None:
            return
        zone = ttk.LabelFrame(frame, text="Skull reference", padding=8)
        zone.pack(fill="x", pady=(8, 0))
        ttk.Label(zone, text="Draw a box around the skull only in this zoomed view.").pack(anchor="w")
        zoom = 3
        photo = tk.PhotoImage(master=zone, data=crop, format="PPM").zoom(zoom)
        canvas = tk.Canvas(zone, width=photo.width(), height=photo.height(), highlightthickness=0)
        canvas.image = photo
        canvas.create_image(0, 0, image=photo, anchor="nw")
        canvas.pack(anchor="w", pady=4)
        controls = ttk.Frame(zone)
        controls.pack(fill="x")
        left, top, right, bottom = skull_search_area(score)
        start = None
        selection = None

        def drag(event):
            nonlocal selection
            if start is None:
                return
            x = min(right, max(left, left + round(event.x / zoom)))
            y = min(bottom, max(top, top + round(event.y / zoom)))
            x1, x2 = sorted((start[0], x))
            y1, y2 = sorted((start[1], y))
            selection = (x1, y1, x2, y2)
            canvas.delete("selection")
            canvas.create_rectangle((x1 - left) * zoom, (y1 - top) * zoom,
                                    (x2 - left) * zoom, (y2 - top) * zoom,
                                    outline="#ffe15c", width=2, tags="selection")
            save_button.configure(state="normal" if x1 < x2 and y1 < y2 else "disabled")

        def begin(event):
            nonlocal start
            start = (left + event.x // zoom, top + event.y // zoom)
            drag(event)

        def save():
            try:
                self.skull_reference = make_skull_reference(image, score, selection)
            except ValueError as error:
                messagebox.showerror("QuickClip", str(error), parent=zone.winfo_toplevel())
                return
            self.save_settings()
            clear_button.configure(state="normal")
            render_details()

        def clear():
            self.skull_reference = None
            self.save_settings()
            clear_button.configure(state="disabled")
            render_details()

        canvas.bind("<Button-1>", begin)
        canvas.bind("<B1-Motion>", drag)
        canvas.bind("<ButtonRelease-1>", drag)
        save_button = ttk.Button(controls, text="Save skull reference", command=save, state="disabled")
        save_button.pack(side="left")
        clear_button = ttk.Button(controls, text="Clear reference", command=clear,
                                  state="normal" if self.skull_reference else "disabled")
        clear_button.pack(side="left", padx=(6, 0))

    def show_feed_results(self):
        lines = []
        for result in sorted(self.feed_results, key=lambda result: result.source.name.casefold()):
            lines.append(result.source.name)
            if result.badge_samples is not None:
                lines.append(f"KILL badge seen in {result.badge_samples} sample(s).")
                lines.append(f"Readable score in {result.badge_score_samples} sample(s).")
                if result.badge_reference_loaded:
                    lines.append(f"Saved skull reference matched {result.badge_reference_matches} sample(s).")
                else:
                    lines.append("No saved skull reference was used for this scan.")
                if result.badge_feed_matches is not None:
                    lines.append(f"Linked to a kill-feed row · {result.badge_feed_matches} notification(s)")
            if not result.feed_scanned:
                lines.append("")
                continue
            lines.append(f"Exact player name seen in {result.feed_mentions} sample(s), "
                         f"{result.feed_matches} matching the selected rule.")
            lines.append(f"Left side · {result.feed_left_mentions} sample(s)   "
                         f"Right side · {result.feed_right_mentions} sample(s)   "
                         f"Side unknown · {result.feed_unknown_mentions} sample(s)")
            if result.feed_green_mentions is not None:
                lines.append(f"Player name marked green in {result.feed_green_mentions} sample(s).")
            if result.feed_lookalikes:
                spellings = ", ".join(f"{value} ({count})" for value, count in result.feed_lookalikes.most_common())
                lines.append(f"Green OCR look-alikes · {spellings}")
            names = Counter()
            for row, count in result.feed_rows.items():
                for name in set(NAME_TOKEN.findall(row)):
                    names[name] += count
            if names:
                names_list = ", ".join(name for name, _ in names.most_common(12))
                lines.append(f"Possible names and words · {names_list}")
                lines.extend(f"  {count} × {line}" for line, count in result.feed_rows.most_common(12))
            else:
                lines.append("No confident name-like text. Use Test feed OCR on a frame with the feed visible.")
            if result.feed_uncertain_rows:
                lines.append(f"Skipped {result.feed_uncertain_rows} low-confidence or non-name OCR row(s).")
            lines.append("")
        self.show_details("Montage OCR readout", "\n".join(lines))

    def warn_review_save(self, error):
        if not self.review_warning_shown:
            self.review_warning_shown = True
            messagebox.showwarning("QuickClip", f"Could not save replay review progress. {error}")

    def restore_replay_list(self):
        saved = self.saved_scan
        self.clear(montage=True)
        self.reviewing_saved_scan = True
        for source in saved.sources:
            if source in saved.results:
                result = saved.results[source]
                self.feed_results.append(result)
                self.append_candidates(result)
        self.load_saved_audio_metadata(saved)
        self.update_keep_count()
        pending = len(saved.pending())
        self.status.set(f"Restored {len(self.clips)} clip(s) from {len(saved.results)} replay(s). "
                        f"{pending} replay(s) still to scan.")
        self.buttons()

    def load_saved_audio_metadata(self, saved):
        missing = [result for result in saved.results.values()
                   if result.audio_tracks is None and result.source not in self.audio_lookup]
        self.audio_lookup.update(result.source for result in missing)

        def worker():
            for result in missing:
                try:
                    count = len(probe(result.source).audio)
                except (OSError, RuntimeError, ValueError):
                    count = None
                self.messages.put(("audio_metadata", (saved, result, count)))

        if missing:
            Thread(target=worker, daemon=True).start()

    def show_audio_metadata(self, saved, result, count):
        self.audio_lookup.discard(result.source)
        if saved is not self.saved_scan or count is None:
            return
        result.audio_tracks = count
        try:
            saved.write_result(result.source)
        except OSError as error:
            self.warn_review_save(error)
        if self.reviewing_saved_scan:
            for index, (source, _) in enumerate(self.clips):
                if source == result.source:
                    self.table.set(str(index), "audio", "No audio" if count == 0 else f"{count} track(s)")

    def resume_saved_scan(self):
        if self.saved_scan is None or self.saved_scan.input_path != self.input_path:
            return
        if self.saved_scan.pending():
            self.start_scan(montage=True, resume=True)
        else:
            self.restore_replay_list()

    def start_scan(self, auto_export=False, montage=False, resume=False):
        player = self.player.get().strip()
        sources = self.sources
        requested = VideoAcceleration(auto_export and self.radeon_encode.get(), self.radeon_decode.get())
        try:
            if resume:
                saved = self.saved_scan
                player, before, after, feed = saved.player, saved.before, saved.after, saved.feed
            else:
                before, after = float(self.before.get()), float(self.after.get())
                if self.from_date.get() or self.to_date.get():
                    sources = filter_recording_dates(self.recording_dates, self.from_date.get(), self.to_date.get())
            audio_offset = self.read_audio_offset() if auto_export else 0
            selection = self.track.current()
            track = "auto" if selection == 0 else selection - 1
            if montage:
                workers = self.montage_workers.get().strip()
                if not workers.isascii() or not workers.isdecimal() or int(workers) < 1:
                    raise ValueError("Enter a positive whole number for the montage scan workers.")
                workers = int(workers)
                if not resume:
                    cue = self.montage_cue.get()
                    badge = cue in MONTAGE_CUES[1:]
                    badge_feed_details = cue == "Badge + feed details"
                    if cue == "KILL badge":
                        player = ""
                    elif not player and not badge_feed_details:
                        raise ValueError("Enter your player name to make a kill montage.")
                    limit = self.montage_limit.get().strip()
                    if limit:
                        if not limit.isascii() or not limit.isdecimal() or int(limit) < 1:
                            raise ValueError("Enter a positive whole number for the montage file limit.")
                        sources = sources[:int(limit)]
                    fallback = (track or "auto") if self.audio_fallback.get() else 0
                    threshold = float(self.threshold.get()) if fallback else -18
                    feed = KillFeedOptions(
                        region=parse_feed_region(self.feed_region.get()) if player or badge_feed_details else FEED,
                        any_mention=self.any_feed_mention.get(), fallback_track=fallback,
                        threshold=threshold, mode=self.feed_mode.get(), badge=badge,
                        skull_reference=self.skull_reference,
                        weapon_references=self.weapon_references,
                        badge_feed_details=badge_feed_details,
                        divider=parse_feed_divider(self.feed_divider.get())
                        if (player or badge_feed_details) and self.fixed_feed_divider.get() else None,
                    )
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
            if not resume:
                try:
                    self.saved_scan = SavedScan.create(self.review_directory, self.input_path, sources,
                                                       player, before, after, feed)
                except OSError as error:
                    messagebox.showerror("QuickClip", f"Could not start saving replay review. {error}")
                    return
            self.restore_replay_list()
            sources = self.saved_scan.pending()
            self.montage_scanning = True
            self.scan_stop = Event()
            self.completed_files = len(self.saved_scan.results)
            self.scan_total = len(self.saved_scan.sources)
            self.update_keep_count()

        def complete(results):
            if montage:
                results = self.feed_results
            else:
                self.clear()
                for result in results:
                    self.append_candidates(result)
            failures = [(result.source, result.errors) for result in results if result.errors]
            status = f"Checked {len(results)} recording(s). Found {len(self.clips)} candidate(s)."
            if montage:
                if self.clips:
                    status += " Play clips and untick any you want to leave out."
                if not failures and player and not feed.badge_feed_details:
                    if not any(NAME_TOKEN.search(line) for result in results for line in result.feed_rows):
                        status += " No confident name-like text. Use Test feed OCR in Set kill-feed area."
                    elif any(result.feed_mentions or result.feed_lookalikes for result in results) and not any(
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
                elif not failures and not self.clips:
                    status += " No on-screen KILL badge was detected."
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
            acceleration = resolve_video_acceleration(requested, progress)
            if montage:
                for result in scan_kills(sources, player, before, after, progress,
                                         workers=workers, stop=self.scan_stop, feed=feed,
                                         acceleration=acceleration):
                    self.messages.put(("montage_result", result))
                return None
            return list(process_batch(sources, options, output, progress, audio_offset_ms=audio_offset,
                                      acceleration=acceleration))

        self.status.set("Scanning recordings…")
        self.work(operation, complete)

    def append_candidates(self, result):
        first = len(self.clips)
        rejected = self.saved_scan.rejections.get(result.source, ()) if self.reviewing_saved_scan else ()
        for position, clip in enumerate(result.clips):
            index = len(self.clips)
            self.clips.append((result.source, clip))
            keep = (self.montage_review and position not in rejected
                    and matches_weapons(clip, parse_weapon_filter(self.only_weapons.get())))
            if keep:
                self.kept.add(index)
            tracks = result.audio_tracks
            audio = "Unknown" if tracks is None else ("No audio" if tracks == 0 else f"{tracks} track(s)")
            details = [reason for reason in clip.reasons if reason.startswith(("Weapon: ", "Target: ", "Victim: "))]
            awards = [reason.removeprefix("Award: ") for reason in clip.reasons if reason.startswith("Award: ")]
            cues = [reason for reason in clip.reasons if reason not in details and not reason.startswith("Award: ")]
            self.table.insert("", "end", iid=str(index), values=(
                ("☑" if keep else "☐") if self.montage_review else "", result.source.name, clock(clip.start),
                clock(clip.end), audio, ", ".join(cues), ", ".join(details) or "Unknown", ", ".join(awards)))
        if self.montage_review and first == 0 and self.clips:
            self.table.selection_set("0")
            self.table.focus("0")

    def apply_preset(self):
        preset = next(preset for preset in self.presets.values() if preset["name"] == self.preset_choice.get())
        self.feed_region.set(preset["feed_region"])
        self.feed_mode.set(preset["feed_mode"])
        if preset.get("montage_cue") in MONTAGE_CUES:
            self.montage_cue.set(preset["montage_cue"])
        self.weapon_references = merge_weapon_references(self.weapon_references, preset["weapon_references"])
        self.save_settings()
        labels = ", ".join(reference.label for reference in preset["weapon_references"])
        self.status.set(f"Applied the {preset['name']} preset: kill-feed area, {preset['feed_mode']} text"
                        + (f", and weapon icons ({labels})." if labels else "."))

    def build_montage(self):
        try:
            audio_offset = self.read_audio_offset()
            mic_mix = self.mic_mix_track.get().strip()
            if mic_mix and (not re.fullmatch(r"[0-9]+", mic_mix) or int(mic_mix) < 1):
                raise ValueError("Enter a positive mic track number for the montage mix, or leave it blank.")
            if self.mic_at_kills.get() and not mic_mix:
                raise ValueError("Enter the mic track number in Mix mic track to use Mic only around kills.")
            style = MontageStyle(transition=self.montage_transition.get(), vertical=self.montage_vertical.get(),
                                 slowmo_multikills=self.slowmo_multikills.get(), mic_at_kills=self.mic_at_kills.get())
        except ValueError as error:
            messagebox.showerror("QuickClip", str(error))
            return
        directory = self.output or self.default_output or self.choose_output()
        if directory is None:
            return
        clips = [self.clips[index] for index in sorted(self.kept)]
        candidates = [BatchResult(source, [clip for _, clip in group], [], [])
                      for source, group in groupby(clips, key=lambda item: item[0])]
        output_format = self.montage_format.get().lower()
        self.start_montage_export(directory, lambda: save_montage_job(
            self.sources, candidates, directory, audio_offset,
            output_format=output_format, mic_mix_track=int(mic_mix) if mic_mix else None, style=style))

    def resume_montage(self):
        directory = self.output or self.default_output
        job = latest_montage_job(directory)
        if job is not None:
            self.start_montage_export(directory, lambda: job)

    def pause_montage(self):
        if self.montage_pause is not None:
            self.montage_pause.set()
            self.status.set("Pausing after the current export step finishes…")
            self.buttons()

    def start_montage_export(self, directory, get_job):
        self.save_settings()
        requested = VideoAcceleration(self.radeon_encode.get(), self.radeon_decode.get())
        pause = Event()
        self.montage_pause = pause

        def operation():
            def progress(message):
                self.messages.put(("progress", message))

            acceleration = resolve_video_acceleration(requested, progress)
            return resume_montage_job(get_job(), directory, progress, acceleration=acceleration, stop=pause)

        def complete(value):
            results, path = value
            failures = [(result.source, result.errors) for result in results if result.errors]
            count = sum(len(result.exports) for result in results)
            if path:
                joined = " · Join 100%" if not failures else ""
                self.status.set(f"Saved {count} clip(s) to {path}{joined}")
            else:
                self.status.set("No montage was created.")
            if failures:
                self.show_failures(failures)
            elif path and self.can_shutdown and self.shutdown_when_finished.get() and not pause.is_set():
                self.start_shutdown_countdown(60)

        self.status.set("Building montage…")
        self.work(operation, complete)

    def start_shutdown_countdown(self, seconds):
        if not self.shutdown_when_finished.get():
            self.cancel_shutdown()
            return
        if seconds > 0:
            self.cancel_shutdown_button.pack(side="right", padx=(0, 6), before=self.keep_label)
            self.status.set(f"Montage saved. Windows will shut down in {seconds}s.")
            self.shutdown_task = self.root.after(1000, self.start_shutdown_countdown, seconds - 1)
            return
        self.shutdown_task = None
        self.shutdown_when_finished.set(False)
        self.cancel_shutdown_button.pack_forget()
        self.save_settings()
        try:
            run(["shutdown.exe", "/s", "/t", "0"])
        except (OSError, RuntimeError) as error:
            self.status.set(f"Montage saved. Windows could not shut down. {error}")

    def cancel_shutdown(self, quiet=False):
        if self.shutdown_task is not None:
            self.root.after_cancel(self.shutdown_task)
            self.shutdown_task = None
            self.shutdown_when_finished.set(False)
            self.cancel_shutdown_button.pack_forget()
            if not quiet:
                self.status.set("Montage saved. Shutdown cancelled.")

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
        requested = VideoAcceleration(self.radeon_encode.get(), self.radeon_decode.get())

        def operation():
            def progress(message):
                self.messages.put(("progress", message))

            acceleration = resolve_video_acceleration(requested, progress)
            if self.default_output is not None:
                prepare_output(self.sources, directory)
            for index, (source, clip) in enumerate(clips, 1):
                progress(f"Exporting clip {index} of {len(clips)}…")
                export_clip(source, clip, directory, progress=progress,
                            audio_offset_ms=audio_offset, acceleration=acceleration)
            return len(clips)

        self.work(operation, lambda count: self.status.set(f"Exported {count} clip(s) to {directory}"))


def launch():
    root = tk.Tk()
    Desktop(root)
    root.mainloop()
