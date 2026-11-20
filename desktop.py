"""Tk desktop interface for QuickClip."""
from itertools import groupby
from pathlib import Path
from queue import Empty, Queue
import shutil
import subprocess
from threading import Thread
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from quickclip import (
    BatchResult, DEFAULT_SCORE, RECORDING_EXTENSIONS, export_clip, folder_recordings,
    prepare_output, probe, process_batch, scan_kills, write_montage,
)


def clock(seconds):
    minutes, remainder = divmod(seconds, 60)
    hours, minutes = divmod(int(minutes), 60)
    return f"{hours:02}:{minutes:02}:{remainder:05.2f}"


class Desktop:
    def __init__(self, root):
        self.root = root
        self.sources = []
        self.default_output = None
        self.output = None
        self.clips = []
        self.montage_review = False
        self.kept = set()
        self.messages = Queue()
        self.status = tk.StringVar(value="Open an OBS recording or folder to get started.")
        self.filename = tk.StringVar(value="No input selected")
        self.output_name = tk.StringVar(value="No output folder selected")
        self.keep_count = tk.StringVar()
        self.score_enabled = tk.BooleanVar()
        self.player = tk.StringVar()
        self.score = tk.StringVar(value=DEFAULT_SCORE)

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
        ttk.Label(actions, textvariable=self.keep_count).pack(side="left", padx=10)
        self.export_button = ttk.Button(actions, text="Export selected…", command=self.export)
        self.export_button.pack(side="right")
        self.build_button = ttk.Button(actions, text="Build montage", command=self.build_montage)
        ttk.Label(body, textvariable=self.status, wraplength=680).pack(fill="x", pady=(10, 0))

        self.busy = False
        self.buttons()
        root.after(100, self.poll)

    def buttons(self):
        ready = bool(self.table.selection()) and not self.busy
        for button in (self.open_button, self.folder_button, self.output_button):
            button.configure(state="disabled" if self.busy else "normal")
        for button in (self.scan_button, self.batch_button, self.montage_button):
            button.configure(state="normal" if self.sources and not self.busy else "disabled")
        self.export_button.configure(state="normal" if ready else "disabled")
        self.build_button.configure(state="normal" if self.kept and not self.busy else "disabled")
        self.preview_button.configure(state="normal" if ready and shutil.which("ffplay") else "disabled")

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
                self.busy = False
                if kind == "error":
                    self.status.set("Operation failed.")
                    messagebox.showerror("QuickClip", value)
                else:
                    complete, result = value
                    complete(result)
                self.buttons()
        except Empty:
            pass
        self.root.after(100, self.poll)

    def clear(self, montage=False):
        self.clips = []
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
        if self.busy:
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
        self.keep_count.set(f"{len(self.kept)} of {len(self.clips)} clips kept")

    def open(self):
        filters = " ".join(f"*{ext}" for ext in RECORDING_EXTENSIONS)
        filename = filedialog.askopenfilename(filetypes=[("Recordings", filters), ("All files", "*")])
        if filename:
            source = Path(filename)
            self.status.set("Reading recording metadata…")
            self.work(lambda: probe(source),
                      lambda info: self.set_input([source], source.name, info, source))

    def open_folder(self):
        directory = filedialog.askdirectory()
        if not directory:
            return
        folder = Path(directory)

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
            self.set_input(sources, f"{folder.name} · {len(sources)} recordings", recording, sample, folder)

        self.status.set("Finding recordings…")
        self.work(inspect, complete)

    def set_input(self, sources, label, recording, sample, folder=None):
        self.clear()
        self.sources = sources
        self.filename.set(label)
        self.default_output = folder / "Highlights" if folder else None
        self.show_output()
        self.track.configure(values=["Auto", "Off"] + [
            f"{index}: {title}" for index, title in enumerate(recording.audio, 1)])
        self.track.current(0)
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
            return self.output
        return None

    def start_scan(self, auto_export=False, montage=False):
        player = self.player.get().strip()
        sources = self.sources
        try:
            before, after = float(self.before.get()), float(self.after.get())
            audio_offset = self.read_audio_offset() if auto_export else 0
            if montage:
                if not player:
                    raise ValueError("Enter your player name to make a kill montage.")
                limit = self.montage_limit.get().strip()
                if limit:
                    if not limit.isascii() or not limit.isdecimal() or int(limit) < 1:
                        raise ValueError("Enter a positive whole number for the montage file limit.")
                    sources = sources[:int(limit)]
            else:
                selection = self.track.current()
                track = "auto" if selection == 0 else selection - 1
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

        def complete(results):
            self.clear(montage=montage)
            exported = 0
            failures = []
            for result in results:
                exported += len(result.exports)
                if result.errors:
                    failures.append((result.source, result.errors))
                for clip in result.clips:
                    index = len(self.clips)
                    self.clips.append((result.source, clip))
                    self.table.insert("", "end", iid=str(index), values=(
                        "☑" if montage else "", result.source.name, clock(clip.start), clock(clip.end),
                        ", ".join(clip.reasons)))
            status = f"Checked {len(results)} recording(s). Found {len(self.clips)} candidate(s)."
            if montage:
                self.kept.update(range(len(self.clips)))
                self.update_keep_count()
                if self.clips:
                    self.table.selection_set("0")
                    self.table.focus("0")
                    status += " Play clips and untick any you want to leave out."
            elif output is not None:
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
                return list(scan_kills(sources, player, before, after, progress))
            return list(process_batch(sources, options, output, progress, audio_offset_ms=audio_offset))

        self.status.set("Scanning recordings…")
        self.work(operation, complete)

    def build_montage(self):
        try:
            audio_offset = self.read_audio_offset()
        except ValueError as error:
            messagebox.showerror("QuickClip", str(error))
            return
        directory = self.output or self.default_output or self.choose_output()
        if directory is None:
            return
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
        window = tk.Toplevel(self.root)
        window.title("Recording errors")
        detail = tk.Text(window, width=85, height=16, wrap="word")
        scrollbar = ttk.Scrollbar(window, command=detail.yview)
        detail.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        detail.pack(side="left", fill="both", expand=True)
        for source, errors in failures:
            detail.insert("end", f"{source.name}\n" + "\n".join(errors) + "\n\n")
        detail.configure(state="disabled")

    def preview(self):
        selected = self.table.selection()
        player = shutil.which("ffplay")
        if self.busy or not selected or not player:
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
