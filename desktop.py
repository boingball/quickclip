"""Tk desktop interface for QuickClip."""
from pathlib import Path
from queue import Empty, Queue
import shutil
import subprocess
from threading import Thread
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from quickclip import (
    DEFAULT_SCORE, RECORDING_EXTENSIONS, export_clip, folder_recordings, prepare_output, probe,
    process_batch,
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
        self.messages = Queue()
        self.status = tk.StringVar(value="Open an OBS recording or folder to get started.")
        self.filename = tk.StringVar(value="No input selected")
        self.output_name = tk.StringVar(value="No output folder selected")
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
        self.track = ttk.Combobox(settings, state="readonly", values=("Off",))
        self.track.current(0)
        self.threshold = ttk.Entry(settings, width=8)
        self.before = ttk.Entry(settings, width=8)
        self.after = ttk.Entry(settings, width=8)
        for field, value in ((self.threshold, "-18"), (self.before, "10"), (self.after, "6")):
            field.insert(0, value)
        settings_fields = (
            ("Microphone track", self.track, 0, 0),
            ("Mic threshold (dBFS)", self.threshold, 0, 2),
            ("Player name (kill feed)", ttk.Entry(settings, textvariable=self.player), 1, 0),
            ("Seconds before", self.before, 1, 2),
            ("Score text pattern", ttk.Entry(settings, textvariable=self.score), 2, 0),
            ("Seconds after", self.after, 2, 2),
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

        results = ttk.Frame(body)
        results.pack(fill="both", expand=True)
        self.table = ttk.Treeview(results, columns=("source", "start", "end", "reasons"), show="headings")
        for name, title, width in (("source", "Recording", 250), ("start", "Start", 100),
                                   ("end", "End", 100), ("reasons", "Detected by", 210)):
            self.table.heading(name, text=title)
            self.table.column(name, width=width, stretch=name in ("source", "reasons"))
        scrollbar = ttk.Scrollbar(results, orient="vertical", command=self.table.yview)
        self.table.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.table.pack(side="left", fill="both", expand=True)
        self.table.bind("<<TreeviewSelect>>", lambda _: self.buttons())
        self.table.bind("<Double-1>", lambda _: self.preview())

        actions = ttk.Frame(body)
        actions.pack(fill="x", pady=(10, 0))
        self.preview_button = ttk.Button(actions, text="Preview", command=self.preview)
        self.preview_button.pack(side="left")
        self.export_button = ttk.Button(actions, text="Export selected…", command=self.export)
        self.export_button.pack(side="right")
        ttk.Label(body, textvariable=self.status, wraplength=680).pack(fill="x", pady=(10, 0))

        self.busy = False
        self.buttons()
        root.after(100, self.poll)

    def buttons(self):
        ready = bool(self.table.selection()) and not self.busy
        for button in (self.open_button, self.folder_button, self.output_button):
            button.configure(state="disabled" if self.busy else "normal")
        for button in (self.scan_button, self.batch_button):
            button.configure(state="normal" if self.sources and not self.busy else "disabled")
        self.export_button.configure(state="normal" if ready else "disabled")
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

    def clear(self):
        self.clips = []
        self.table.delete(*self.table.get_children())

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
        self.track.configure(values=["Off"] + [
            f"{index}: {title}" for index, title in enumerate(recording.audio, 1)])
        self.track.current(0)
        if folder:
            self.status.set(f"Audio tracks shown from {sample.name}. "
                            "Choose a mic track that is in the same position across your recordings.")
        else:
            self.status.set(f"{clock(recording.duration)} · {len(recording.audio)} audio track(s). "
                            "Choose the isolated mic track or enable on-screen detection.")

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

    def start_scan(self, auto_export=False):
        try:
            options = (self.track.current(), float(self.threshold.get()), self.player.get(),
                       self.score.get().strip() if self.score_enabled.get() else "",
                       float(self.before.get()), float(self.after.get()))
            if self.score_enabled.get() and not options[3]:
                raise ValueError("Enter a score text pattern.")
        except ValueError as error:
            messagebox.showerror("QuickClip", str(error))
            return

        output = None
        if auto_export:
            output = self.output or self.default_output or self.choose_output()
            if output is None:
                return

        def complete(results):
            self.clear()
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
                        result.source.name, clock(clip.start), clock(clip.end),
                        ", ".join(clip.reasons)))
            status = f"Checked {len(results)} recording(s). Found {len(self.clips)} candidate(s)."
            if output is not None:
                status += f" Exported {exported} to {output}."
            if failures:
                status += f" {len(failures)} recording(s) had errors."
            self.status.set(status)
            if failures:
                self.show_failures(failures)

        self.status.set("Scanning recordings…")
        self.work(lambda: list(process_batch(self.sources, options, output,
                                            progress=lambda msg: self.messages.put(("progress", msg)))),
                  complete)

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

    def export(self):
        clips = [self.clips[int(index)] for index in self.table.selection()]
        directory = self.output or self.default_output or self.choose_output()
        if directory is None:
            return

        def operation():
            if self.default_output is not None:
                prepare_output(self.sources, directory)
            for index, (source, clip) in enumerate(clips, 1):
                self.messages.put(("progress", f"Exporting clip {index} of {len(clips)}…"))
                export_clip(source, clip, directory)
            return len(clips)

        self.work(operation, lambda count: self.status.set(f"Exported {count} clip(s) to {directory}"))


def launch():
    root = tk.Tk()
    Desktop(root)
    root.mainloop()
