#!/usr/bin/env python3
"""
Anime3rb Episode Scraper

Fetches all episode direct video URLs from an Anime3rb anime page.

Usage:
    python anime_scraper.py https://anime3rb.com/titles/{anime-slug}

Output:
    anime.json - JSON file with all episode data and video URLs

How it works:
    1. Fetch the titles page and extract all episode links
    2. For each episode page, extract the player URL from Livewire JSON
    3. Fetch the player page (video.vid3rb.com) and extract the direct MP4
       URL from the embedded video_sources JavaScript variable
    4. Return the highest quality (720p) video source
"""

import json
import re
import sys
import time
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Referer": "https://anime3rb.com/",
}


def extract_anime_slug(url: str) -> str | None:
    """Extract the anime slug from a titles page URL."""
    pattern = r"anime3rb\.com/titles/([^/#?]+)"
    match = re.search(pattern, url)
    return match.group(1) if match else None


def extract_episode_number(link: str, slug: str) -> int | None:
    """Extract the episode number from an episode link URL."""
    pattern = rf"/episode/{re.escape(slug)}/(\d+)"
    match = re.search(pattern, link)
    return int(match.group(1)) if match else None


def fetch_page(url: str) -> str | None:
    """Fetch a page and return its HTML content."""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        return resp.text
    except requests.RequestException as e:
        print(f"  [!] Failed to fetch {url}: {e}")
        return None


def get_episode_links(titles_url: str) -> list[dict]:
    """
    Parse the titles page and extract all episode links.
    Returns list of dicts with 'url' and 'number' keys.
    """
    html = fetch_page(titles_url)
    if not html:
        return []

    soup = BeautifulSoup(html, "html.parser")
    slug = extract_anime_slug(titles_url)
    if not slug:
        print("  [!] Could not extract anime slug from URL")
        return []

    episode_links = []
    seen = set()

    for a_tag in soup.find_all("a", href=True):
        href = a_tag.get("href", "").strip()
        number = extract_episode_number(href, slug)
        if number is not None and href not in seen:
            seen.add(href)
            episode_links.append({"url": href, "number": number})

    episode_links.sort(key=lambda x: x["number"])
    return episode_links


def extract_player_url_from_livewire(html: str) -> str | None:
    """
    Extract the iframe player URL from Livewire wire:snapshot JSON data.
    Returns something like:
        https://video.vid3rb.com/player/{uuid}?token=...&expires=...
    """
    pattern = r'wire:snapshot="([^"]+)"'
    for match in re.finditer(pattern, html):
        try:
            raw = match.group(1)
            raw = raw.replace("&quot;", '"').replace("&#039;", "'").replace("&amp;", "&")
            data = json.loads(raw)
            video_url = data.get("data", {}).get("video_url")
            if video_url and "video.vid3rb.com" in video_url:
                return video_url
        except (json.JSONDecodeError, AttributeError, KeyError):
            continue
    return None


def extract_direct_video_src(player_page_html: str) -> str | None:
    """
    Parse the player page HTML and extract the direct MP4 URL
    from the embedded video_sources JavaScript variable.

    The player page contains:
        var video_sources = [{"src":"https://...","type":"video/mp4","label":"720p",...}, ...]

    Returns the highest quality (720p) video URL, or None if not found.
    """
    # Method 1: Extract the video_sources JSON array
    pattern = r'var\s+video_sources\s*=\s*(\[[\s\S]*?\]);'
    match = re.search(pattern, player_page_html)
    if match:
        try:
            raw_json = match.group(1)
            sources = json.loads(raw_json)
            if sources:
                for src in sources:
                    if src.get("label") == "720p" and src.get("src"):
                        return src["src"].replace("\\/", "/")
                first_src = sources[0].get("src")
                if first_src:
                    return first_src.replace("\\/", "/")
        except (json.JSONDecodeError, IndexError, KeyError):
            pass

    # Method 2: Fallback regex for embedded URLs
    fallback = re.search(
        r'https:\\/\\/video\.vid3rb\.com\\/video\\/[^"\'\\]+',
        player_page_html,
    )
    if fallback:
        return fallback.group(0).replace("\\/", "/")

    return None


def extract_video_url(episode_url: str) -> str | None:
    """
    Extract the direct video MP4 URL from an episode page.

    Pipeline:
        1. Fetch the episode page
        2. Extract the player URL from Livewire wire:snapshot data
        3. Fetch the player page
        4. Extract the direct video src from video_sources
    """
    html = fetch_page(episode_url)
    if not html:
        return None

    # Check for static <video> tag first
    soup = BeautifulSoup(html, "html.parser")
    video_tag = soup.find("video", id="video_html5_api")
    if video_tag and video_tag.get("src"):
        src = video_tag["src"].strip()
        if src:
            print(f"    [✓] Found <video> tag with src")
            return src

    # Get the player URL from Livewire data
    player_url = extract_player_url_from_livewire(html)
    if not player_url:
        print(f"    [✗] No player URL found in Livewire data")
        return None

    # Fetch the player page
    print(f"    [~] Fetching player page...")
    player_html = fetch_page(player_url)
    if not player_html:
        print(f"    [✗] Failed to fetch player page")
        return None

    # Extract the direct video source
    direct_src = extract_direct_video_src(player_html)
    if direct_src:
        print(f"    [✓] Direct video URL found: {direct_src[:60]}...")
        return direct_src

    print(f"    [✗] No video source found in player page")
    return None


def scrape_anime(titles_url: str) -> dict:
    """Main function: scrape all episodes for a given anime titles URL."""
    slug = extract_anime_slug(titles_url)
    if not slug:
        print(f"[!] Invalid URL: {titles_url}")
        print("    Expected format: https://anime3rb.com/titles/{anime-slug}")
        sys.exit(1)

    print(f"[*] Anime slug detected: {slug}")
    print(f"[*] Fetching episode list from: {titles_url}")

    episode_links = get_episode_links(titles_url)
    if not episode_links:
        print("[!] No episode links found on the page.")
        sys.exit(1)

    print(f"[*] Found {len(episode_links)} episode(s)\n")

    result = {"anime": slug, "episodes": []}

    for i, ep in enumerate(episode_links, 1):
        ep_num = ep["number"]
        ep_url = ep["url"]
        print(f"  [{i}/{len(episode_links)}] Episode {ep_num}: {ep_url}")
        video_url = extract_video_url(ep_url)

        result["episodes"].append(
            {
                "episode": ep_num,
                "page_url": ep_url,
                "video_url": video_url,
            }
        )

        time.sleep(0.5)

    return result


def main():
    if len(sys.argv) < 2:
        print("Usage: python anime_scraper.py https://anime3rb.com/titles/{anime-slug}")
        sys.exit(1)

    titles_url = sys.argv[1].strip().rstrip("/")

    parsed = urlparse(titles_url)
    if "anime3rb.com" not in parsed.netloc or "/titles/" not in parsed.path:
        print("[!] Invalid URL. Must be an Anime3rb titles page:")
        print("    https://anime3rb.com/titles/{anime-slug}")
        sys.exit(1)

    result = scrape_anime(titles_url)

    output_file = "anime.json"
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)

    print(f"\n[*] Done! Saved {len(result['episodes'])} episode(s) to {output_file}")

    found = sum(1 for ep in result["episodes"] if ep["video_url"])
    missing = len(result["episodes"]) - found
    print(f"[*] Summary: {found} video URLs found, {missing} missing")


if __name__ == "__main__":
    main()
