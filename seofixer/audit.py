"""Read a public webpage and run basic SEO checks. No AI calls happen here."""

import re
import socket
from ipaddress import ip_address
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup
from django.conf import settings


FOLD = 800  # pixels visible without scrolling in our 1280x800 browser


TOTAL_CHECKS = 14  # number of rules below, used for "rule checks passed"


# Blocking these hides the page from that engine's results and AI answers.
SEARCH_BOTS = ["Googlebot", "Bingbot", "OAI-SearchBot", "Claude-SearchBot", "PerplexityBot"]
# Blocking these only stops AI training; search is not affected.
TRAINING_BOTS = ["GPTBot", "ClaudeBot", "Google-Extended"]


SCROLL_TO_BOTTOM_JS = """async () => {
    const end = Math.min(document.body.scrollHeight, 15000);
    for (let y = 0; y < end; y += 600) { window.scrollTo(0, y); await new Promise(r => setTimeout(r, 120)); }
    window.scrollTo(0, 0);
}"""


MEASURE_IMAGES_JS = """() => Array.from(document.images).slice(0, 60).map(img => {
    const box = img.getBoundingClientRect();
    const entry = performance.getEntriesByName(img.currentSrc)[0];
    return {
        src: img.getAttribute("src") || "",
        url: img.currentSrc || img.src,
        natural_width: img.naturalWidth,
        shown_width: Math.round(box.width),
        top: Math.round(box.top + window.scrollY),
        loading: img.getAttribute("loading") || "",
        fetchpriority: img.getAttribute("fetchpriority") || "",
        bytes: entry ? (entry.encodedBodySize || entry.transferSize || 0) : 0,
    };
})"""


def validate_url(url):
    """This demo audits public HTTP(S) pages, not files or private services."""
    parsed = urlparse(url)
    if (parsed.scheme not in ("http", "https") or not parsed.hostname
            or parsed.username or parsed.password or parsed.port not in (None, 80, 443)):
        raise ValueError("Enter a public http:// or https:// URL on port 80 or 443.")
    try:
        addresses = socket.getaddrinfo(parsed.hostname, parsed.port or 443, type=socket.SOCK_STREAM)
    except OSError as error:
        raise ValueError("Could not resolve this website's address.") from error
    if not addresses or any(not ip_address(item[4][0]).is_global for item in addresses):
        raise ValueError("Private and local addresses cannot be audited.")
    return url


def fetch_page(url, trusted_origin=None):
    """Render the page, measure images at two widths, and take a screenshot.

    trusted_origin is the CLI's own local preview server: the only private address allowed."""
    from playwright.sync_api import Error, sync_playwright
    trusted = lambda u: bool(trusted_origin) and u.startswith(trusted_origin)
    if not trusted(url):
        validate_url(url)
    image_bytes = {}

    def allow_public_request(route):
        try:
            if not trusted(route.request.url):
                validate_url(route.request.url)
            # Do not follow redirects inside fetch: the browser routes each next URL.
            response = route.fetch(max_redirects=0, timeout=30000)
        except (ValueError, Error):  # a blocked or broken file must not stall the whole page
            route.abort()
            return
        if response.headers.get("content-type", "").startswith("image/"):
            image_bytes[route.request.url] = len(response.body())
        route.fulfill(response=response)

    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            context = browser.new_context(viewport={"width": 1280, "height": FOLD}, service_workers="block")
            context.route("**/*", allow_public_request)
            context.route_web_socket("**/*", lambda socket: socket.close())
            page = context.new_page()
            response = page.goto(url, wait_until="load", timeout=30000)
            if response is None or response.status >= 400:
                raise ValueError("The website returned an error instead of a page.")
            page.wait_for_timeout(1000)
            page.evaluate(SCROLL_TO_BOTTOM_JS)
            page.wait_for_timeout(500)
            images = page.evaluate(MEASURE_IMAGES_JS)
            for image in images:
                image["bytes"] = image_bytes.get(image["url"], image["bytes"])
            rendered_html = page.content()
            height = min(page.evaluate("document.documentElement.scrollHeight"), 15000)
            screenshot = page.screenshot(clip={"x": 0, "y": 0, "width": 1280, "height": height}, animations="disabled")
            page.set_viewport_size({"width": 1920, "height": 1080})
            page.wait_for_timeout(300)
            wide = page.evaluate("() => Array.from(document.images).slice(0, 60).map(i => Math.round(i.getBoundingClientRect().width))")
            for image, width in zip(images, wide):
                image["shown_width"] = max(image["shown_width"], width)
            return {"html": rendered_html, "url": page.url, "images": images, "screenshot": screenshot}
        finally:
            browser.close()


def extract_page_data(rendered_html, url):
    soup = BeautifulSoup(rendered_html, "html.parser")

    # Read structured data before we throw scripts away.
    structured_data_blocks = len(soup.find_all("script", type="application/ld+json"))
    data_nosnippet = len(soup.find_all(attrs={"data-nosnippet": True}))
    for tag in soup(["script", "style", "noscript", "svg", "iframe", "template"]):
        tag.decompose()

    def meta(name):
        tag = soup.find("meta", attrs={"name": re.compile(f"^{name}$", re.I)})  # "Description" counts too
        return tag.get("content") if tag else None

    canonical = soup.find("link", rel="canonical")
    site_host = urlparse(url).netloc
    images = soup.find_all("img")
    links = soup.find_all("a", href=True)
    vague_words = {"click here", "here", "read more", "more", "learn more", "link"}
    body_text = " ".join(soup.body.get_text(" ", strip=True).split()) if soup.body else ""

    return {
        "url": url,
        "lang": soup.html.get("lang") if soup.html else None,
        "title": soup.title.get_text(strip=True) if soup.title else None,
        "meta_description": meta("description"),
        "meta_robots": meta("robots"),
        "has_viewport": meta("viewport") is not None,
        "canonical": canonical.get("href") if canonical else None,
        "structured_data_blocks": structured_data_blocks,
        "data_nosnippet": data_nosnippet,
        "headings": [[h.name, h.get_text(" ", strip=True)[:120]]
                     for h in soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6"])],
        "image_count": len(images),
        "images_missing_alt": [img.get("src") for img in images
                               if not img.has_attr("alt")][:20],
        "images_missing_alt_count": sum(1 for img in images if not img.has_attr("alt")),
        "internal_links": sum(1 for a in links
                              if urlparse(urljoin(url, a["href"])).netloc == site_host
                              and urlparse(urljoin(url, a["href"])).path != urlparse(url).path),
        "external_links": sum(1 for a in links
                              if urlparse(urljoin(url, a["href"])).netloc not in ("", site_host)),
        "vague_link_texts": sorted({a.get_text(strip=True) for a in links
                                    if a.get_text(strip=True).lower() in vague_words}),
        "word_count": len(body_text.split()),
        "body_text": body_text[:settings.MAX_BODY_CHARS],
    }


def read_robots(url):
    """Which crawlers robots.txt blocks from this page. Empty when robots.txt cannot be read."""
    try:
        response = requests.get(urljoin(url, "/robots.txt"), timeout=10, allow_redirects=False)
    except requests.RequestException:
        return {}
    if response.is_redirect:  # not followed (it could point anywhere), so the answer is unknown
        return {}
    parser = RobotFileParser()
    parser.parse(response.text.splitlines() if response.status_code == 200 else [])  # no robots.txt: all allowed
    # shortcut: Python's parser applies the first matching rule, Google the longest; mixed Allow/Disallow can differ.
    blocked = lambda bots: [bot for bot in bots if not parser.can_fetch(bot, url)]
    return {"blocked_search_bots": blocked(SEARCH_BOTS), "blocked_training_bots": blocked(TRAINING_BOTS),
            "sitemap_listed": bool(parser.site_maps())}


def run_checks(data):
    """Observable checks, not a Google ranking score or a quality guarantee."""
    issues = []
    def add(area, severity, detail):
        issues.append({"area": area, "severity": severity, "detail": detail})

    if not data["title"]:
        add("snippets", "high", "Missing page title.")
    if not data["meta_description"]:
        add("snippets", "medium", "Missing meta description; consider a useful page summary.")
    if not any(level == "h1" for level, text in data["headings"]):
        add("content", "low", "No H1; review whether the main heading is clear.")
    levels = [int(level[1]) for level, text in data["headings"]]
    if any(b - a > 1 for a, b in zip(levels, levels[1:])):
        add("content", "low", "Heading levels skip; review the page outline.")
    if data["images_missing_alt_count"]:
        add("images", "medium", f"{data['images_missing_alt_count']} images lack an alt attribute. Decorative images can use empty alt text.")
    if not data["lang"]:
        add("images", "low", "Missing document language attribute.")
    if not data["has_viewport"]:
        add("technical", "medium", "Missing viewport meta tag; check the mobile layout.")
    if not data["canonical"]:
        add("technical", "low", "No canonical link; review if duplicate URLs need consolidation.")
    robots = re.split(r"[\s,]+", (data["meta_robots"] or "").lower())
    if "noindex" in robots:
        add("technical", "high", "Page has noindex. Confirm whether exclusion from search is intentional.")
    if "nosnippet" in robots or "max-snippet:0" in robots or data.get("data_nosnippet"):
        add("technical", "medium", "Snippet limits (nosnippet, max-snippet or data-nosnippet) stop Google and its AI answers from quoting this page.")
    if data.get("blocked_search_bots"):
        add("technical", "high" if "Googlebot" in data["blocked_search_bots"] else "medium",
            "robots.txt blocks " + ", ".join(data["blocked_search_bots"]) + ": this page cannot appear in their search results or AI answers.")
    if data.get("sitemap_listed") is False:
        add("technical", "low", "robots.txt does not list a sitemap; add a 'Sitemap:' line so crawlers find every page.")
    if data["vague_link_texts"]:
        add("technical", "low", "Some links use vague text: " + ", ".join(data["vague_link_texts"]))
    if not data["internal_links"]:
        add("technical", "low", "No links to other internal paths; review whether more navigation would help.")
    return issues
