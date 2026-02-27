import asyncio
import threading
import time
from urllib.parse import urlparse, urljoin

from flask import Flask, render_template, request, Response, abort
from playwright.async_api import async_playwright
from bs4 import BeautifulSoup
from feedgen.feed import FeedGenerator
import requests

# -------------------------------------------------
# Flask Setup
# -------------------------------------------------

app = Flask(__name__)

# -------------------------------------------------
# Dedicated Async Loop (for Playwright)
# -------------------------------------------------

playwright_loop = asyncio.new_event_loop()
browser = None


def start_background_loop(loop):
    asyncio.set_event_loop(loop)
    loop.run_forever()


threading.Thread(
    target=start_background_loop,
    args=(playwright_loop,),
    daemon=True
).start()

# -------------------------------------------------
# Start Playwright Browser ONCE
# -------------------------------------------------

async def start_browser():
    global browser
    p = await async_playwright().start()
    browser = await p.chromium.launch(headless=True)


future = asyncio.run_coroutine_threadsafe(start_browser(), playwright_loop)
future.result()

# -------------------------------------------------
# Run Async Safely From Flask Thread
# -------------------------------------------------

def run_async(coro):
    future = asyncio.run_coroutine_threadsafe(coro, playwright_loop)
    return future.result()

# -------------------------------------------------
# Simple Cache (5 min TTL)
# -------------------------------------------------

CACHE = {}
CACHE_TTL = 300  # seconds


def get_cache_key(url, item_css, title_css):
    return f"{url}|{item_css}|{title_css}"


def get_cached_feed(key):
    data = CACHE.get(key)
    if not data:
        return None
    if time.time() - data["time"] > CACHE_TTL:
        del CACHE[key]
        return None
    return data["value"]


def set_cached_feed(key, value):
    CACHE[key] = {
        "value": value,
        "time": time.time()
    }

# -------------------------------------------------
# Security: Basic URL Validation
# -------------------------------------------------

def is_valid_url(url):
    parsed = urlparse(url)
    if parsed.scheme not in ["http", "https"]:
        return False
    if parsed.hostname in ["localhost", "127.0.0.1"]:
        return False
    return True

# -------------------------------------------------
# Core Scraper
# -------------------------------------------------

async def scrape_website(url, item_selector, title_selector):
    context = await browser.new_context(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
    )
    page = await context.new_page()

    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await page.evaluate("window.scrollTo(0, 1200)")
        await asyncio.sleep(1)
        content = await page.content()
    finally:
        await context.close()

    soup = BeautifulSoup(content, "html.parser")

    fg = FeedGenerator()
    fg.title(f"Custom RSS Feed: {url}")
    fg.link(href=url)
    fg.description(f"Generated RSS feed from {url}")
    fg.language("en")
    fg.lastBuildDate()

    items = soup.select(item_selector)[:20]

    for item in items:
        title_el = item.select_one(title_selector)
        link_el = item.find("a", href=True)

        if title_el and link_el:
            full_link = urljoin(url, link_el["href"])

            fe = fg.add_entry()
            fe.title(title_el.get_text(strip=True))
            fe.link(href=full_link)
            fe.guid(full_link, permalink=True)
            fe.description(title_el.get_text(strip=True))
            fe.pubDate()

    return fg.rss_str(pretty=True)

# -------------------------------------------------
# Routes
# -------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/preview")
def preview():
    target_url = request.args.get("url")

    if not target_url:
        return "Missing URL", 400

    if not is_valid_url(target_url):
        return "Invalid URL", 400

    try:
        r = requests.get(
            target_url,
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=15
        )

        soup = BeautifulSoup(r.text, "html.parser")

        # Fix relative paths
        for tag in soup.find_all(["link", "script", "img"]):
            if tag.get("src"):
                tag["src"] = urljoin(target_url, tag["src"])
            if tag.get("href"):
                tag["href"] = urljoin(target_url, tag["href"])

        # Inject selector script
        injection = """
        <script>
        document.addEventListener('click', function(e) {
            e.preventDefault();
            e.stopPropagation();

            let el = e.target;
            let selector = el.tagName.toLowerCase();

            if (el.classList.length) {
                selector += "." + [...el.classList].join(".");
            }

            window.parent.postMessage({
                type: 'SELECTOR',
                value: selector
            }, '*');

        }, true);
        </script>
        """

        return str(soup) + injection

    except Exception as e:
        return f"Preview Error: {str(e)}", 500


@app.route("/feed")
def serve_feed():
    url = request.args.get("url")
    item_css = request.args.get("item")
    title_css = request.args.get("title")

    if not all([url, item_css, title_css]):
        abort(400, "Missing parameters")

    if not is_valid_url(url):
        abort(400, "Invalid URL")

    cache_key = get_cache_key(url, item_css, title_css)
    cached = get_cached_feed(cache_key)

    if cached:
        return Response(cached, mimetype="application/xml")

    try:
        rss_data = run_async(scrape_website(url, item_css, title_css))
        set_cached_feed(cache_key, rss_data)
        return Response(rss_data, mimetype="application/xml")
    except Exception as e:
        abort(500, f"Feed generation failed: {str(e)}")

# -------------------------------------------------
# Run App
# -------------------------------------------------

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)