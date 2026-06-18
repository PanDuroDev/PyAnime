#!/usr/bin/env python3
"""
Anime3rb Scraper GUI — CustomTkinter Desktop Application

Provides a user-friendly interface for the anime_scraper module.
"""

import json
import os
import queue
import sys
import threading
import time
import webbrowser
from tkinter import filedialog, messagebox
from urllib.parse import urlparse

import customtkinter as ctk

from anime_scraper import (
    extract_anime_slug,
    extract_video_url,
    get_episode_links,
)

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")


class ThreadSafeLogger:
    """Thread-safe logger that queues messages for the GUI log widget."""

    def __init__(self, text_widget, app):
        self.text_widget = text_widget
        self.app = app
        self.queue = queue.Queue()
        self._poll()

    def write(self, message):
        if message.strip():
            self.queue.put(message)

    def flush(self):
        pass

    def _poll(self):
        try:
            while True:
                msg = self.queue.get_nowait()
                self.text_widget.insert(ctk.END, msg)
                self.text_widget.see(ctk.END)
        except queue.Empty:
            pass
        self.app.after(100, self._poll)


class AnimeScraperApp(ctk.CTk):
    def __init__(self):
        super().__init__()

        # ── Window setup ──────────────────────────────────────────────
        self.title("Anime3rb Scraper")
        self.geometry("950x750")
        self.minsize(750, 550)

        # State
        self.scraping = False
        self.results = None

        # ── Layout ────────────────────────────────────────────────────
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)

        # ── Top: URL input ────────────────────────────────────────────
        top_frame = ctk.CTkFrame(self, fg_color="transparent")
        top_frame.grid(row=0, column=0, sticky="ew", padx=16, pady=(16, 4))
        top_frame.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(top_frame, text="Anime URL:", font=("Segoe UI", 14)).grid(
            row=0, column=0, sticky="w", padx=(0, 8)
        )
        self.url_entry = ctk.CTkEntry(
            top_frame,
            placeholder_text="https://anime3rb.com/titles/{anime-slug}",
            height=38,
            font=("Segoe UI", 13),
        )
        self.url_entry.grid(row=0, column=1, sticky="ew", padx=(0, 8))

        self.scrape_btn = ctk.CTkButton(
            top_frame,
            text="🚀 Scrape",
            command=self.start_scrape,
            width=110,
            height=38,
            font=("Segoe UI", 13, "bold"),
        )
        self.scrape_btn.grid(row=0, column=2, sticky="e")

        # ── Middle: Progress log ──────────────────────────────────────
        ctk.CTkLabel(
            self, text="📋 Progress Log", anchor="w", font=("Segoe UI", 13, "bold")
        ).grid(row=1, column=0, sticky="w", padx=16, pady=(8, 0))

        self.log_text = ctk.CTkTextbox(
            self, height=130, font=("Consolas", 12), wrap="word"
        )
        self.log_text.grid(row=2, column=0, sticky="nsew", padx=16, pady=(0, 8))

        self.logger = ThreadSafeLogger(self.log_text, self)

        # ── Results table ─────────────────────────────────────────────
        table_header_frame = ctk.CTkFrame(self, fg_color="transparent")
        table_header_frame.grid(row=3, column=0, sticky="ew", padx=16, pady=(4, 0))
        table_header_frame.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            table_header_frame,
            text="📺 Episodes",
            font=("Segoe UI", 13, "bold"),
        ).pack(side="left")

        self.quality_label = ctk.CTkLabel(
            table_header_frame,
            text="(720p MP4)",
            font=("Segoe UI", 11),
            text_color="gray",
        )
        self.quality_label.pack(side="left", padx=(6, 0))

        # Scrollable table
        self.table_frame = ctk.CTkScrollableFrame(self, height=280, corner_radius=8)
        self.table_frame.grid(row=4, column=0, sticky="nsew", padx=16, pady=(4, 8))
        self.table_frame.grid_columnconfigure((0, 1, 2, 3), weight=1)
        self.table_frame.grid_columnconfigure(1, weight=2)
        self.table_frame.grid_columnconfigure(2, weight=4)
        self.table_frame.grid_columnconfigure(3, weight=0)

        # Column headers
        header_colors = ["#", "Episode", "Video URL", "Open"]
        header_weights = [1, 2, 4, 0]
        header_frame = ctk.CTkFrame(self.table_frame, fg_color="transparent", height=32)
        header_frame.grid(row=0, column=0, columnspan=4, sticky="ew", pady=(0, 4))
        header_frame.grid_columnconfigure((0, 1, 2, 3), weight=1)
        header_frame.grid_columnconfigure(1, weight=2)
        header_frame.grid_columnconfigure(2, weight=4)
        header_frame.grid_columnconfigure(3, weight=0)

        for col, text in enumerate(header_colors):
            lbl = ctk.CTkLabel(
                header_frame,
                text=text,
                font=("Segoe UI", 11, "bold"),
                anchor="w",
            )
            lbl.grid(row=0, column=col, sticky="w", padx=4)

        self.result_rows = []
        self._show_empty_state()

        # ── Bottom bar ────────────────────────────────────────────────
        bottom_frame = ctk.CTkFrame(self, fg_color="transparent")
        bottom_frame.grid(row=5, column=0, sticky="ew", padx=16, pady=(0, 12))
        bottom_frame.grid_columnconfigure(0, weight=1)

        self.save_btn = ctk.CTkButton(
            bottom_frame,
            text="💾 Save JSON",
            command=self.save_results,
            width=110,
            state="disabled",
        )
        self.save_btn.pack(side="right", padx=(8, 0))

        self.clear_btn = ctk.CTkButton(
            bottom_frame, text="🗑 Clear", command=self.clear_results, width=90
        )
        self.clear_btn.pack(side="right")

        self.status_label = ctk.CTkLabel(
            bottom_frame, text="✨ Ready", anchor="w", font=("Segoe UI", 12)
        )
        self.status_label.pack(side="left", fill="x", expand=True)

        self.url_entry.bind("<Return>", lambda e: self.start_scrape())

    # ── Helpers ───────────────────────────────────────────────────────

    def _show_empty_state(self):
        for w in self.result_rows:
            w.destroy()
        self.result_rows.clear()

        empty = ctk.CTkLabel(
            self.table_frame,
            text="No results yet. Enter a URL and click Scrape.",
            font=("Segoe UI", 12),
            text_color="gray",
        )
        empty.grid(row=1, column=0, columnspan=4, sticky="ew", padx=4, pady=30)
        self.result_rows.append(empty)

    def _populate_table(self, episodes: list[dict]):
        self._show_empty_state()

        for i, ep in enumerate(episodes, start=1):
            has_url = bool(ep["video_url"])
            status = "✅" if has_url else "❌"
            ep_label = f"{status}  Ep {ep['episode']}"
            video_url = ep["video_url"] or "—"

            row_frame = ctk.CTkFrame(self.table_frame, fg_color="transparent", height=30)
            row_frame.grid(row=i, column=0, columnspan=4, sticky="ew", pady=1)
            row_frame.grid_columnconfigure((0, 1, 2, 3), weight=1)
            row_frame.grid_columnconfigure(1, weight=2)
            row_frame.grid_columnconfigure(2, weight=4)
            row_frame.grid_columnconfigure(3, weight=0)

            ctk.CTkLabel(row_frame, text=str(i), anchor="w", font=("Consolas", 11)).grid(
                row=0, column=0, sticky="w", padx=4
            )
            ctk.CTkLabel(row_frame, text=ep_label, anchor="w", font=("Segoe UI", 11)).grid(
                row=0, column=1, sticky="w", padx=4
            )
            ctk.CTkLabel(
                row_frame,
                text=video_url[:55] + "..." if len(video_url) > 55 else video_url,
                anchor="w",
                font=("Consolas", 10),
                text_color=("green" if has_url else "gray"),
            ).grid(row=0, column=2, sticky="w", padx=4)

            if has_url:
                open_btn = ctk.CTkButton(
                    row_frame,
                    text="▶",
                    width=30,
                    height=24,
                    font=("Segoe UI", 12),
                    command=lambda u=ep["video_url"]: webbrowser.open(u),
                )
                open_btn.grid(row=0, column=3, sticky="e", padx=(0, 4))
            else:
                ctk.CTkLabel(row_frame, text="", width=30).grid(
                    row=0, column=3, sticky="e", padx=(0, 4)
                )

            self.result_rows.append(row_frame)

    # ── Core actions ──────────────────────────────────────────────────

    def start_scrape(self):
        if self.scraping:
            return

        url = self.url_entry.get().strip().rstrip("/")
        if not url:
            messagebox.showwarning("Input Error", "Please enter an Anime3rb URL.")
            return

        parsed = urlparse(url)
        if "anime3rb.com" not in parsed.netloc or "/titles/" not in parsed.path:
            messagebox.showwarning(
                "Invalid URL",
                "URL must be an Anime3rb titles page:\n"
                "https://anime3rb.com/titles/{anime-slug}",
            )
            return

        self.scraping = True
        self.scrape_btn.configure(state="disabled", text="⏳ Scraping...")
        self.save_btn.configure(state="disabled")
        self.status_label.configure(text="⏳ Scraping in progress...")
        self.log_text.delete("0.0", ctk.END)
        self._show_empty_state()
        self.results = None

        threading.Thread(target=self._scrape_worker, args=(url,), daemon=True).start()

    def _scrape_worker(self, url: str):
        try:
            slug = extract_anime_slug(url)
            if not slug:
                self._dispatch_error("Could not extract anime slug from URL.")
                return

            self._dispatch_log(f"[*] Anime: {slug}")
            self._dispatch_log("[*] Fetching episode list...")

            episode_links = get_episode_links(url)
            if not episode_links:
                self._dispatch_error("No episode links found.")
                return

            self._dispatch_log(f"[*] Found {len(episode_links)} episode(s)\n")

            results = {"anime": slug, "episodes": []}

            for i, ep in enumerate(episode_links, 1):
                ep_num = ep["number"]
                ep_url = ep["url"]
                self._dispatch_log(f"  [{i}/{len(episode_links)}] Ep {ep_num}...")
                video_url = extract_video_url(ep_url)

                results["episodes"].append(
                    {
                        "episode": ep_num,
                        "page_url": ep_url,
                        "video_url": video_url,
                    }
                )
                time.sleep(0.5)

            self._dispatch_log(f"\n[*] Scraping complete!")
            found = sum(1 for e in results["episodes"] if e["video_url"])
            self._dispatch_log(f"[*] {found}/{len(results['episodes'])} video URLs found")
            self._dispatch_done(results)

        except Exception as e:
            self._dispatch_error(f"Error: {e}")

    # ── Thread-safe dispatch helpers ──────────────────────────────────

    def _dispatch_log(self, msg: str):
        self.logger.write(msg + "\n")

    def _dispatch_done(self, results: dict):
        self.after(0, self._on_scrape_done, results)

    def _dispatch_error(self, msg: str):
        self._dispatch_log(f"[!] {msg}")
        self.after(0, self._on_scrape_error, msg)

    def _on_scrape_done(self, results: dict):
        self.scraping = False
        self.results = results
        self.scrape_btn.configure(state="normal", text="🚀 Scrape")
        self.save_btn.configure(state="normal")
        self.status_label.configure(
            text=f"✅ {results['anime']} — {len(results['episodes'])} episodes"
        )
        self.quality_label.configure(text=f"(720p MP4 ✓)")
        self._populate_table(results["episodes"])

    def _on_scrape_error(self, msg: str):
        self.scraping = False
        self.scrape_btn.configure(state="normal", text="🚀 Scrape")
        self.save_btn.configure(state="disabled")
        self.status_label.configure(text=f"❌ {msg}")
        messagebox.showerror("Scrape Error", msg)

    # ── Save / Clear ──────────────────────────────────────────────────

    def save_results(self):
        if not self.results:
            return

        default_name = f"{self.results['anime']}.json"
        file_path = filedialog.asksaveasfilename(
            defaultextension=".json",
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")],
            initialfile=default_name,
        )
        if not file_path:
            return

        try:
            with open(file_path, "w", encoding="utf-8") as f:
                json.dump(self.results, f, indent=2, ensure_ascii=False)
            self.status_label.configure(text=f"💾 Saved to {os.path.basename(file_path)}")
            messagebox.showinfo("Saved", f"Results saved to:\n{file_path}")
        except Exception as e:
            messagebox.showerror("Save Error", str(e))

    def clear_results(self):
        self.results = None
        self._show_empty_state()
        self.log_text.delete("0.0", ctk.END)
        self.status_label.configure(text="✨ Cleared")
        self.save_btn.configure(state="disabled")
        self.scrape_btn.configure(state="normal", text="🚀 Scrape")
        self.quality_label.configure(text="(720p MP4)")

    def on_closing(self):
        if self.scraping:
            if not messagebox.askokcancel("Quit", "Scraping in progress. Quit anyway?"):
                return
        self.destroy()


def main():
    app = AnimeScraperApp()
    app.protocol("WM_DELETE_WINDOW", app.on_closing)
    app.mainloop()


if __name__ == "__main__":
    main()
