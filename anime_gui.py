#!/usr/bin/env python3
"""
Anime3rb Scraper Pro  —  Material Design 3
High-performance rewrite:
  * Virtual list (CTkTextbox-based) instead of per-episode widgets
  * Batched UI updates (flush every 200 ms, not per-item)
  * Zero unnecessary after(0) calls during scraping
  * DPI-aware, crisp rendering with Tk scaling locked
"""

import json
import os
import queue
import re
import threading
import tkinter as tk
import webbrowser
from tkinter import filedialog, messagebox
from urllib.parse import urlparse

import customtkinter as ctk
import requests
from bs4 import BeautifulSoup

# ── Appearance ─────────────────────────────────────────────
ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("material_theme.json")

# ── Colours (MD3 dark palette) ─────────────────────────────
C_BG       = "#1C1B1F"
C_SURFACE  = "#2B2930"
C_SURFACE2 = "#32303A"
C_PRIMARY  = "#D0BCFF"
C_ON_PRI   = "#1C1B1F"
C_ERROR    = "#B3261E"
C_ERROR_H  = "#8C1D18"
C_TEXT     = "#E6E1E5"
C_SUBTEXT  = "#938F99"
C_DIVIDER  = "#38353C"


# ════════════════════════════════════════════════════════════
#  HTTP helpers
# ════════════════════════════════════════════════════════════

def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ar,en-US;q=0.7,en;q=0.3",
        "Referer":         "https://anime3rb.com/",
        "Upgrade-Insecure-Requests": "1",
    })
    return s


def fetch_page(session: requests.Session, url: str) -> str | None:
    try:
        r = session.get(url, timeout=30)
        r.raise_for_status()
        return r.text
    except requests.RequestException:
        return None


# ════════════════════════════════════════════════════════════
#  Parsing
# ════════════════════════════════════════════════════════════

def extract_slug(url: str) -> str | None:
    m = re.search(r"anime3rb\.com/titles/([^/#?]+)", url)
    return m.group(1) if m else None


def extract_ep_num(link: str, slug: str) -> int | None:
    m = re.search(rf"/episode/{re.escape(slug)}/(\d+)", link)
    return int(m.group(1)) if m else None


def normalize(href: str) -> str:
    return href if href.startswith("http") else "https://anime3rb.com" + href


# Known block / captcha indicators
_BLOCK_SIGNATURES = [
    "cf-browser-verification",  # Cloudflare challenge
    "cf_chl_",                  # Cloudflare captcha
    "Just a moment",            # CF waiting room
    "Attention Required",       # CF/other firewall
    "Access denied",
    "403 Forbidden",
    "limit-reached",
    "vpn",                      # Explicit VPN block
    "Please enable JavaScript", # JS-only page (no content)
    "<title>Error</title>",
]


def _detect_block(html: str) -> str | None:
    """Return a human-readable reason if the page looks blocked, else None."""
    lower = html[:4000].lower()
    if "cf-browser-verification" in lower or 'id="cf-' in lower:
        return "Cloudflare challenge detected (try without VPN or wait a moment)"
    if "just a moment" in lower:
        return "Cloudflare waiting room — the site needs a real browser to pass the check"
    if "access denied" in lower or "403 forbidden" in lower:
        return "Access denied (403) — your IP or VPN is blocked by the site"
    if "limit-reached" in lower:
        return "Rate-limit page returned — the site is throttling your IP"
    if "please enable javascript" in lower:
        return "Site returned a JS-only page — try disabling VPN or using a different IP"
    # Page looks almost empty (< 2 KB)
    if len(html.strip()) < 1500:
        return "Page returned unusually small content — possible block or redirect"
    return None


def get_episodes(url: str, session: requests.Session, on_log=None) -> tuple[list[dict], str | None]:
    """
    Returns (episodes, error_msg).
    episodes is empty on error; error_msg explains what happened.
    """
    html = fetch_page(session, url)
    if html is None:
        return [], "Failed to fetch the page (network error or timeout)."

    # Check for block before parsing
    block_reason = _detect_block(html)
    if block_reason:
        return [], block_reason

    slug = extract_slug(url)
    if not slug:
        return [], "Cannot extract anime slug from URL."

    seen: set[str] = set()
    eps: list[dict] = []
    soup = BeautifulSoup(html, "html.parser")
    for a in soup.find_all("a", href=True):
        h = a["href"].strip()
        n = extract_ep_num(h, slug)
        if n is not None and h not in seen:
            seen.add(h)
            eps.append({"episode": n, "page_url": normalize(h)})

    if not eps:
        # Try to diagnose: check page title
        title_tag = soup.find("title")
        title = title_tag.get_text(strip=True) if title_tag else "(no title)"
        if on_log:
            on_log(f"[?] Page title: {title}")
            on_log(f"[?] Page length: {len(html)} chars")
        return [], (
            f"No episode links found on the page.\n"
            f"Page title: \"{title}\"\n"
            "This usually means:\n"
            "  • VPN IP is blocked by the site\n"
            "  • URL is wrong (check /titles/ vs /anime/)\n"
            "  • The anime has no episodes yet"
        )

    eps.sort(key=lambda x: x["episode"])
    return eps, None


# ════════════════════════════════════════════════════════════
#  Scraping engine  (background thread, batched callbacks)
# ════════════════════════════════════════════════════════════

class ScrapingEngine:
    def __init__(self):
        self._cancel = threading.Event()
        self.browser = None

    def cancel(self):
        self._cancel.set()
        try:
            if self.browser:
                self.browser.close()
        except Exception:
            pass

    def scrape(self, url: str, on_log, on_done):
        self._cancel.clear()
        self.browser = None
        threading.Thread(target=self._run, args=(url, on_log, on_done), daemon=True).start()

    def _run(self, url, on_log, on_done):
        from playwright.sync_api import sync_playwright
        import time

        try:
            slug = extract_slug(url)
            if not slug:
                on_done(None, "Cannot extract slug from URL.")
                return

            on_log(f"[*] Anime: {slug}")
            on_log("[*] Launching Chrome browser...")
            
            playwright_obj = None
            browser_instance = None
            try:
                playwright_obj = sync_playwright().start()
                
                # Try Chrome, then Edge, then default Chromium
                for channel in ["chrome", "msedge"]:
                    if self._cancel.is_set():
                        break
                    try:
                        on_log(f"[*] Trying browser: {channel}")
                        browser_instance = playwright_obj.chromium.launch(headless=False, channel=channel)
                        break
                    except Exception:
                        pass
                
                if not browser_instance and not self._cancel.is_set():
                    try:
                        on_log("[*] Fallback: Trying default playwright chromium...")
                        browser_instance = playwright_obj.chromium.launch(headless=False)
                    except Exception as e:
                        on_done(None, f"Could not launch any browser: {e}")
                        playwright_obj.stop()
                        return

                if self._cancel.is_set():
                    if browser_instance:
                        browser_instance.close()
                    playwright_obj.stop()
                    on_done(None, "Cancelled.")
                    return

                self.browser = browser_instance
                context = self.browser.new_context()
                page = context.new_page()
                page.set_viewport_size({"width": 1280, "height": 800})
                
                on_log("[*] Loading anime title page...")
                page.goto(url)
                
                # Wait for Cloudflare challenge to pass
                success = False
                for i in range(30):
                    if self._cancel.is_set():
                        break
                    title = page.title()
                    content = page.content()
                    if "Just a moment" not in title and "Attention Required" not in title and "cloudflare" not in content.lower():
                        success = True
                        break
                    time.sleep(1.0)
                    
                if self._cancel.is_set():
                    self.browser.close()
                    playwright_obj.stop()
                    on_done(None, "Cancelled.")
                    return
                    
                if not success:
                    self.browser.close()
                    playwright_obj.stop()
                    on_done(None, "Failed to bypass Cloudflare on title page.")
                    return

                on_log("[*] Extracting episode links...")
                html = page.content()
                seen = set()
                eps = []
                soup = BeautifulSoup(html, "html.parser")
                for a in soup.find_all("a", href=True):
                    h = a["href"].strip()
                    n = extract_ep_num(h, slug)
                    if n is not None and h not in seen:
                        seen.add(h)
                        eps.append({"episode": n, "page_url": normalize(h)})
                
                eps.sort(key=lambda x: x["episode"])
                
                if not eps:
                    self.browser.close()
                    playwright_obj.stop()
                    on_done(None, "No episode links found on page.")
                    return
                    
                on_log(f"[*] Found {len(eps)} episodes. Extracting video streams...")
                
                for idx, ep in enumerate(eps):
                    if self._cancel.is_set():
                        break
                    ep_num = ep["episode"]
                    ep_url = ep["page_url"]
                    
                    on_log(f"[*] Episode {ep_num}/{len(eps)}: Loading...")
                    try:
                        page.goto(ep_url)
                        
                        video_url = None
                        direct_url = None
                        
                        # Wait up to 10 seconds for video tag inside iframe
                        for _ in range(10):
                            if self._cancel.is_set():
                                break
                            frames = page.frames
                            player_frame = next((f for f in frames if "vid3rb.com" in f.url or "player" in f.url), None)
                            if player_frame:
                                try:
                                    video_tag = player_frame.locator("video#video_html5_api")
                                    if video_tag.count() > 0:
                                        src = video_tag.get_attribute("src")
                                        if src:
                                            video_url = src
                                            break
                                except Exception:
                                    pass
                            time.sleep(1.0)
                            
                        # Extract direct download link
                        ep_html = page.content()
                        ep_soup = BeautifulSoup(ep_html, "html.parser")
                        for a in ep_soup.find_all("a", href=True):
                            href = a["href"].strip()
                            if "/download/" in href and not href.endswith("/download"):
                                direct_url = href if href.startswith("http") else "https://anime3rb.com" + href
                                break
                        
                        # Fallback to player hash if direct video link was not loaded (daily limit)
                        if not video_url:
                            video_source = None
                            all_snaps = re.findall(r'wire:snapshot="([^"]+)"', ep_html)
                            for s in all_snaps:
                                raw = s.replace("&quot;", '"').replace("&amp;", "&").replace("&#039;", "'")
                                try:
                                    data = json.loads(raw)
                                    if data.get("memo", {}).get("name") == "video.show-video":
                                        video_source = data.get("data", {}).get("video_source")
                                        break
                                except Exception:
                                    pass
                            
                            if video_source:
                                video_url = f"https://video.vid3rb.com/player/{video_source}"
                                on_log(f"    [!] Daily limit active. Using player fallback.")
                            else:
                                on_log(f"    [!] Failed to parse player link.")
                                
                        ep["video_url"] = video_url
                        ep["player_url"] = video_url
                        ep["direct_url"] = direct_url
                        
                        if video_url:
                            on_log(f"    [+] Player/Video URL extracted.")
                        if direct_url:
                            on_log(f"    [+] Direct download URL extracted.")
                            
                    except Exception as ep_err:
                        on_log(f"    [!] Error: {ep_err}")
                        ep["video_url"] = None
                        ep["player_url"] = None
                        ep["direct_url"] = None
                        
                self.browser.close()
                playwright_obj.stop()
                
                if self._cancel.is_set():
                    on_done(None, "Cancelled.")
                else:
                    on_done({"anime": slug, "episodes": eps}, None)
                    
            except Exception as inner_exc:
                if playwright_obj:
                    try:
                        playwright_obj.stop()
                    except Exception:
                        pass
                raise inner_exc
                
        except Exception as exc:
            on_done(None, str(exc))


# ════════════════════════════════════════════════════════════
#  Fast virtual episode list  (uses a single Text widget)
# ════════════════════════════════════════════════════════════

class EpisodeList(tk.Frame):
    """
    Renders episodes as clickable lines in a single Text widget —
    orders of magnitude faster than one Frame+Label per episode.
    """
    ROW_H   = 26      # px per row (approx)
    PAD_X   = 10

    def __init__(self, master, **kw):
        super().__init__(master, bg=C_SURFACE, **kw)
        self._episodes: list[dict] = []
        self._build()

    def _build(self):
        # Header bar
        hdr = tk.Frame(self, bg=C_SURFACE2, height=32)
        hdr.pack(fill="x")
        tk.Label(hdr, text="  #", bg=C_SURFACE2, fg=C_SUBTEXT,
                 font=("Segoe UI", 10, "bold"), anchor="w", width=5).pack(side="left")
        tk.Label(hdr, text="Page URL", bg=C_SURFACE2, fg=C_SUBTEXT,
                 font=("Segoe UI", 10, "bold"), anchor="w").pack(side="left", fill="x", expand=True)
        tk.Label(hdr, text="Open  ", bg=C_SURFACE2, fg=C_SUBTEXT,
                 font=("Segoe UI", 10, "bold")).pack(side="right")

        # Text widget  (virtual list)
        txt_frame = tk.Frame(self, bg=C_SURFACE)
        txt_frame.pack(fill="both", expand=True)

        self._txt = tk.Text(
            txt_frame,
            bg=C_SURFACE, fg=C_TEXT,
            font=("Consolas", 11),
            cursor="arrow",
            relief="flat",
            bd=0,
            padx=self.PAD_X,
            pady=4,
            wrap="none",
            state="disabled",
            selectbackground=C_SURFACE2,
            highlightthickness=0,
        )
        sb = tk.Scrollbar(txt_frame, orient="vertical", command=self._txt.yview,
                          bg=C_SURFACE, troughcolor=C_SURFACE2, relief="flat", width=8)
        self._txt.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self._txt.pack(side="left", fill="both", expand=True)

        # Tags
        self._txt.tag_configure("ep",  foreground=C_SUBTEXT, font=("Consolas", 11))
        self._txt.tag_configure("url", foreground=C_PRIMARY,  font=("Consolas", 11),
                                underline=False)
        self._txt.tag_configure("url_hover", foreground="#EADDFF", underline=True)
        self._txt.tag_configure("even", background=C_SURFACE)
        self._txt.tag_configure("odd",  background="#2E2C33")

        self._txt.bind("<Motion>",   self._on_motion)
        self._txt.bind("<Button-1>", self._on_click)
        self._txt.bind("<Leave>",    self._on_leave)
        self._hover_line: int | None = None

    # ── Public API ──────────────────────────────────────────

    def set_episodes(self, episodes: list[dict]):
        self._episodes = episodes
        self._render()

    def clear(self):
        self._episodes = []
        self._txt.configure(state="normal")
        self._txt.delete("1.0", "end")
        self._txt.configure(state="disabled")

    # ── Rendering ───────────────────────────────────────────

    def _render(self):
        self._txt.configure(state="normal")
        self._txt.delete("1.0", "end")
        for i, ep in enumerate(self._episodes):
            bg_tag = "even" if i % 2 == 0 else "odd"
            ep_lbl = f"  Ep {ep['episode']:>3}   "
            url    = ep["page_url"]
            line   = i + 1
            self._txt.insert("end", ep_lbl,   (f"ep_{line}", "ep",  bg_tag))
            self._txt.insert("end", url,       (f"url_{line}", "url", bg_tag))
            self._txt.insert("end", "\n",      bg_tag)
        self._txt.configure(state="disabled")

    # ── Interaction ─────────────────────────────────────────

    def _line_at(self, event) -> int:
        idx = self._txt.index(f"@{event.x},{event.y}")
        return int(idx.split(".")[0])

    def _on_motion(self, event):
        try:
            line = self._line_at(event)
        except Exception:
            return
        if line == self._hover_line:
            return
        if self._hover_line is not None:
            self._txt.tag_remove("url_hover", f"{self._hover_line}.0", f"{self._hover_line}.end")
        self._hover_line = line
        if 1 <= line <= len(self._episodes):
            self._txt.tag_add("url_hover", f"{line}.0", f"{line}.end")
            self._txt.configure(cursor="hand2")
        else:
            self._txt.configure(cursor="arrow")

    def _on_leave(self, event):
        if self._hover_line is not None:
            self._txt.tag_remove("url_hover", f"{self._hover_line}.0", f"{self._hover_line}.end")
            self._hover_line = None
        self._txt.configure(cursor="arrow")

    def _on_click(self, event):
        try:
            line = self._line_at(event)
        except Exception:
            return
        if 1 <= line <= len(self._episodes):
            url = self._episodes[line - 1]["page_url"]
            webbrowser.open(url)


# ════════════════════════════════════════════════════════════
#  Scrape Tab
# ════════════════════════════════════════════════════════════

class ScrapeTab(ctk.CTkFrame):
    PAD = 12

    def __init__(self, master, tab_name: str, **kw):
        super().__init__(master, **kw)
        self.tab_name  = tab_name
        self.scraping  = False
        self.results   = None
        self.engine    = ScrapingEngine()
        self._log_q: queue.Queue[str] = queue.Queue()

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(3, weight=1)
        self._build_url_bar()
        self._build_progress()
        self._build_log()
        self._build_results()
        self._poll_log()

    # ── Layout builders ─────────────────────────────────────

    def _build_url_bar(self):
        card = ctk.CTkFrame(self, corner_radius=16)
        card.grid(row=0, column=0, sticky="ew", padx=self.PAD, pady=(10, 4))
        card.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(card, text="Anime URL",
                     font=("Segoe UI", 13, "bold")).grid(
            row=0, column=0, sticky="w", padx=(16, 6), pady=(12, 0))
        ctk.CTkLabel(card, text="anime3rb.com/titles/{slug}",
                     font=("Segoe UI", 10), text_color=C_SUBTEXT).grid(
            row=1, column=0, sticky="w", padx=(16, 6), pady=(0, 12))

        self.url_entry = ctk.CTkEntry(
            card,
            placeholder_text="https://anime3rb.com/titles/anime-slug",
            height=42, font=("Segoe UI", 12))
        self.url_entry.grid(row=0, column=1, rowspan=2, sticky="ew", padx=(0, 8), pady=10)

        self.scrape_btn = ctk.CTkButton(
            card, text="Scrape ▶", command=self.start_scrape,
            width=110, height=42, font=("Segoe UI", 13, "bold"))
        self.scrape_btn.grid(row=0, column=2, rowspan=2, padx=(0, 8), pady=10)

        self.cancel_btn = ctk.CTkButton(
            card, text="✕ Cancel", command=self.cancel_scrape,
            width=90, height=42,
            fg_color=C_ERROR, hover_color=C_ERROR_H,
            font=("Segoe UI", 12, "bold"), state="disabled")
        self.cancel_btn.grid(row=0, column=3, rowspan=2, padx=(0, 12), pady=10)
        self.url_entry.bind("<Return>", lambda _: self.start_scrape())

    def _build_progress(self):
        f = ctk.CTkFrame(self, fg_color="transparent")
        f.grid(row=1, column=0, sticky="ew", padx=self.PAD, pady=(2, 0))
        f.grid_columnconfigure(0, weight=1)

        self.progress = ctk.CTkProgressBar(f, height=5, corner_radius=3)
        self.progress.grid(row=0, column=0, sticky="ew")
        self.progress.set(0)

        self.status_lbl = ctk.CTkLabel(
            f, text="Ready", anchor="w",
            font=("Segoe UI", 11), text_color=C_SUBTEXT)
        self.status_lbl.grid(row=1, column=0, sticky="ew", pady=(1, 0))

    def _build_log(self):
        card = ctk.CTkFrame(self, corner_radius=16)
        card.grid(row=2, column=0, sticky="ew", padx=self.PAD, pady=(4, 4))
        card.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(card, text="Log", font=("Segoe UI", 11, "bold")).grid(
            row=0, column=0, sticky="w", padx=14, pady=(8, 2))
        self.log_box = ctk.CTkTextbox(
            card, height=72, font=("Consolas", 10), wrap="word",
            state="disabled")
        self.log_box.grid(row=1, column=0, sticky="ew", padx=14, pady=(0, 8))

    def _build_results(self):
        card = ctk.CTkFrame(self, corner_radius=16)
        card.grid(row=3, column=0, sticky="nsew", padx=self.PAD, pady=(2, 10))
        card.grid_columnconfigure(0, weight=1)
        card.grid_rowconfigure(1, weight=1)

        # Header row
        hdr = ctk.CTkFrame(card, fg_color="transparent")
        hdr.grid(row=0, column=0, sticky="ew", padx=14, pady=(10, 4))

        ctk.CTkLabel(hdr, text="Episodes", font=("Segoe UI", 13, "bold")).pack(side="left")
        self.count_lbl = ctk.CTkLabel(
            hdr, text="", font=("Segoe UI", 11), text_color=C_SUBTEXT)
        self.count_lbl.pack(side="left", padx=(8, 0))

        self.clear_btn = ctk.CTkButton(
            hdr, text="Clear", command=self.clear_results,
            width=72, height=28,
            fg_color=C_ERROR, hover_color=C_ERROR_H, font=("Segoe UI", 11))
        self.clear_btn.pack(side="right")

        self.save_btn = ctk.CTkButton(
            hdr, text="Save JSON", command=self.save_results,
            width=95, height=28, state="disabled", font=("Segoe UI", 11))
        self.save_btn.pack(side="right", padx=(0, 8))

        # Episode list
        self.ep_list = EpisodeList(card)
        self.ep_list.grid(row=1, column=0, sticky="nsew", padx=14, pady=(0, 10))

        self._empty_lbl = ctk.CTkLabel(
            card, text="Enter a URL above and click Scrape ▶",
            font=("Segoe UI", 12), text_color=C_SUBTEXT)
        self._empty_lbl.grid(row=1, column=0, sticky="nsew")
        self.ep_list.grid_remove()

    # ── Log helpers ─────────────────────────────────────────

    def _log(self, msg: str):
        self._log_q.put(msg)

    def _poll_log(self):
        # Drain entire queue at once  →  single widget update
        msgs = []
        try:
            while True:
                msgs.append(self._log_q.get_nowait())
        except queue.Empty:
            pass
        if msgs:
            self.log_box.configure(state="normal")
            self.log_box.insert("end", "\n".join(msgs) + "\n")
            self.log_box.see("end")
            self.log_box.configure(state="disabled")
        self.after(150, self._poll_log)

    # ── Scrape lifecycle ─────────────────────────────────────

    def start_scrape(self):
        if self.scraping:
            return
        url = self.url_entry.get().strip().rstrip("/")
        if not url:
            messagebox.showwarning("Notice", "Please enter an Anime3rb URL.")
            return
        p = urlparse(url)
        if "anime3rb.com" not in p.netloc or "/titles/" not in p.path:
            messagebox.showwarning(
                "Notice", "URL must be:\nhttps://anime3rb.com/titles/{slug}")
            return

        self.scraping = True
        self.results  = None
        self.scrape_btn.configure(state="disabled", text="Scraping…")
        self.cancel_btn.configure(state="normal")
        self.save_btn.configure(state="disabled")
        self.count_lbl.configure(text="")
        self.status_lbl.configure(text="Fetching…")
        self.progress.set(0)

        self.log_box.configure(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.configure(state="disabled")
        self.ep_list.clear()
        self._empty_lbl.grid()
        self.ep_list.grid_remove()

        self.engine.scrape(
            url,
            on_log  = lambda m: self._log_q.put(m),
            on_done = lambda r, e: self.after(0, self._on_done, r, e),
        )
        # animate indeterminate progress
        self._animate_progress(0)

    def _animate_progress(self, step: int):
        if not self.scraping:
            return
        self.progress.set((step % 100) / 100)
        self.after(30, self._animate_progress, step + 2)

    def cancel_scrape(self):
        self.engine.cancel()
        self._log("[!] Cancelling…")

    def _on_done(self, results, error):
        self.scraping = False
        self.scrape_btn.configure(state="normal", text="Scrape ▶")
        self.cancel_btn.configure(state="disabled")
        self.progress.set(0)

        if error:
            if error != "Cancelled.":
                self._log(f"[!] {error}")
                first_line = error.splitlines()[0]
                self.status_lbl.configure(text=f"⚠  {first_line}")
                self._show_error_dialog(error)
            else:
                self.status_lbl.configure(text="⚠  Cancelled")
            return

        self._on_results(results)

    def _show_error_dialog(self, msg: str):
        """Show a styled error dialog with full diagnostic info."""
        dlg = ctk.CTkToplevel(self)
        dlg.title("Scrape Error")
        dlg.resizable(False, False)
        dlg.grab_set()
        dlg.focus_set()

        # Icon + title
        top = ctk.CTkFrame(dlg, fg_color="#B3261E", corner_radius=0)
        top.pack(fill="x")
        ctk.CTkLabel(
            top, text="⚠  Scrape Failed",
            font=("Segoe UI", 14, "bold"), text_color="white",
        ).pack(padx=20, pady=12)

        # Message body
        body = ctk.CTkFrame(dlg, fg_color="transparent")
        body.pack(fill="both", padx=20, pady=(12, 4))

        ctk.CTkTextbox(
            body, font=("Segoe UI", 12),
            wrap="word", width=420, height=180,
            state="normal",
            fg_color="#2B2930",
        ).pack(fill="both", expand=True)
        # insert then disable
        txt = body.winfo_children()[-1]
        txt.insert("1.0", msg)
        txt.configure(state="disabled")

        # Tip box
        tip = ctk.CTkFrame(dlg, fg_color="#2B2930", corner_radius=10)
        tip.pack(fill="x", padx=20, pady=(4, 4))
        ctk.CTkLabel(
            tip,
            text="💡  Tip: Disable your VPN and try again. anime3rb.com blocks many VPN/proxy IPs.",
            font=("Segoe UI", 11), text_color="#CAC4D0",
            wraplength=400, justify="left",
        ).pack(padx=12, pady=8)

        ctk.CTkButton(
            dlg, text="OK", command=dlg.destroy,
            width=100, height=34, font=("Segoe UI", 12, "bold"),
        ).pack(pady=(4, 16))

        # Center over parent
        dlg.update_idletasks()
        x = self.winfo_rootx() + (self.winfo_width()  - dlg.winfo_width())  // 2
        y = self.winfo_rooty() + (self.winfo_height() - dlg.winfo_height()) // 2
        dlg.geometry(f"+{x}+{y}")

    def _on_results(self, results):
        self.results = results
        eps   = results["episodes"]
        name  = results["anime"]
        count = len(eps)

        self.save_btn.configure(state="normal")
        self.status_lbl.configure(text=f"✔  {name}  —  {count} episodes")
        self.count_lbl.configure(text=f"{count} eps")
        self.progress.set(1)

        # Single bulk render  —  no per-episode after() calls
        self._empty_lbl.grid_remove()
        self.ep_list.grid()
        self.ep_list.set_episodes(eps)
        self._log(f"[*] Done — {count} episodes")


    # ── Save / Clear ─────────────────────────────────────────

    def save_results(self):
        if not self.results:
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".json",
            filetypes=[("JSON files", "*.json")],
            initialfile=f"{self.results['anime']}.json",
        )
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(self.results, fh, indent=2, ensure_ascii=False)
            self.status_lbl.configure(text=f"Saved  {os.path.basename(path)}")
            messagebox.showinfo("Saved", f"Saved:\n{path}")
        except OSError as exc:
            messagebox.showerror("Save Error", str(exc))

    def clear_results(self):
        if self.scraping:
            self.cancel_scrape()
        self.results = None
        self.ep_list.clear()
        self.ep_list.grid_remove()
        self._empty_lbl.grid()
        self.count_lbl.configure(text="")
        self.status_lbl.configure(text="Cleared")
        self.progress.set(0)
        self.save_btn.configure(state="disabled")
        self.scrape_btn.configure(state="normal", text="Scrape ▶")
        self.log_box.configure(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.configure(state="disabled")


# ════════════════════════════════════════════════════════════
#  Main Window  (multi-tab)
# ════════════════════════════════════════════════════════════

class AnimeScraperApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        # DPI / scaling
        self.tk.call("tk", "scaling", 1.0)

        self.title("Anime3rb Scraper Pro")
        self.geometry("1060x820")
        self.minsize(840, 620)
        self._n = 0

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)

        self.tabs = ctk.CTkTabview(self, corner_radius=16)
        self.tabs.grid(row=0, column=0, sticky="nsew", padx=10, pady=(10, 4))

        bar = ctk.CTkFrame(self, fg_color="transparent", height=40)
        bar.grid(row=1, column=0, sticky="ew", padx=10, pady=(0, 8))

        ctk.CTkButton(bar, text="＋  New Tab", command=self.add_tab,
                      width=110, height=34,
                      font=("Segoe UI", 12, "bold")).pack(side="left")
        ctk.CTkButton(bar, text="✕  Close Tab", command=self.close_tab,
                      width=110, height=34,
                      fg_color=C_ERROR, hover_color=C_ERROR_H,
                      font=("Segoe UI", 12)).pack(side="left", padx=(8, 0))
        ctk.CTkLabel(bar,
                     text="anime3rb.com  —  scrapes episode + page_url",
                     font=("Segoe UI", 11), text_color=C_SUBTEXT).pack(side="right")

        self.add_tab()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def add_tab(self):
        self._n += 1
        name = f"Tab {self._n}"
        self.tabs.add(name)
        ScrapeTab(self.tabs.tab(name), name).pack(fill="both", expand=True)
        self.tabs.set(name)

    def close_tab(self):
        cur  = self.tabs.get()
        tabs = list(self.tabs._segmented_button._buttons_dict.keys())
        if len(tabs) <= 1:
            messagebox.showinfo("Info", "Cannot close the last tab.")
            return
        self._cancel_tab(cur)
        self.tabs.delete(cur)

    def _cancel_tab(self, name: str):
        try:
            for w in self.tabs.tab(name).winfo_children():
                if isinstance(w, ScrapeTab) and w.scraping:
                    w.cancel_scrape()
        except Exception:
            pass

    def _on_close(self):
        for n in list(self.tabs._segmented_button._buttons_dict.keys()):
            self._cancel_tab(n)
        self.destroy()


def main():
    app = AnimeScraperApp()
    app.mainloop()


if __name__ == "__main__":
    main()