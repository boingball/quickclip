"""Calendar and relative date shortcuts for recording filters."""
import calendar
from datetime import date, timedelta
import tkinter as tk
from tkinter import messagebox, ttk


def pick_dates(parent, first, last, oldest, newest, apply):
    window = tk.Toplevel(parent)
    window.title("Recording date range")
    window.transient(parent)
    window.grab_set()
    content = ttk.Frame(window, padding=12)
    content.pack(fill="both", expand=True)
    ttk.Label(content, text=f"Folder dates · {oldest} to {newest}").pack(anchor="w")

    starts = tk.StringVar(window, value=first)
    ends = tk.StringVar(window, value=last)
    shortcuts = ttk.Frame(content)
    shortcuts.pack(fill="x", pady=6)
    today = date.today()

    def set_dates(start, end):
        starts.set(str(start))
        ends.set(str(end))

    for label, start, end in (
        ("All dates", "", ""), ("Today", today, today),
        ("Yesterday", today - timedelta(days=1), today - timedelta(days=1)),
        ("This week", today - timedelta(days=today.weekday()), today),
        ("Folder range", oldest, newest),
    ):
        ttk.Button(shortcuts, text=label, command=lambda start=start, end=end: set_dates(start, end)).pack(
            side="left", padx=(0, 4))

    calendars = ttk.Frame(content)
    calendars.pack()

    def calendar_picker(title, selected, initial):
        frame = ttk.LabelFrame(calendars, text=title, padding=6)
        frame.pack(side="left", padx=4)
        ttk.Entry(frame, textvariable=selected, width=14).pack()
        navigation = ttk.Frame(frame)
        navigation.pack(fill="x", pady=4)
        grid = ttk.Frame(frame)
        grid.pack()
        month = initial.replace(day=1)
        caption = ttk.Label(navigation, anchor="center", width=17)

        def render():
            caption.configure(text=month.strftime("%B %Y"))
            for child in grid.winfo_children():
                child.destroy()
            for col, label in enumerate(("Mo", "Tu", "We", "Th", "Fr", "Sa", "Su")):
                ttk.Label(grid, text=label, anchor="center", width=4).grid(row=0, column=col)
            for row, week in enumerate(calendar.Calendar().monthdayscalendar(month.year, month.month), 1):
                for col, day in enumerate(week):
                    if day:
                        value = month.replace(day=day).isoformat()
                        button = ttk.Button(grid, text=str(day), width=3, command=lambda value=value: selected.set(value))
                        button.grid(row=row, column=col)
                        if selected.get() == value:
                            button.state(["pressed"])

        def change(offset):
            nonlocal month
            index = month.year * 12 + month.month - 1 + offset
            year, number = divmod(index, 12)
            if 1 <= year <= 9999:
                month = date(year, number + 1, 1)
                render()

        def follow(*_):
            nonlocal month
            try:
                month = date.fromisoformat(selected.get()).replace(day=1)
            except ValueError:
                pass
            render()

        ttk.Button(navigation, text="‹", width=3, command=lambda: change(-1)).pack(side="left")
        caption.pack(side="left", expand=True)
        ttk.Button(navigation, text="›", width=3, command=lambda: change(1)).pack(side="right")
        selected.trace_add("write", follow)
        follow()

    calendar_picker("From", starts, oldest)
    calendar_picker("To", ends, newest)

    def confirm():
        try:
            apply(starts.get().strip(), ends.get().strip())
        except ValueError as error:
            messagebox.showerror("QuickClip", str(error), parent=window)
            return
        window.destroy()

    ttk.Button(content, text="Apply dates", command=confirm).pack(anchor="e", pady=(6, 0))
