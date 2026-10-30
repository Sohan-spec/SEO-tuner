"""Collect scores and image measurements, then compare before and after."""

import base64
import io
import re
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import requests
from django.conf import settings

from . import audit


HEAVY_IMAGE_BYTES = 150_000


PAGESPEED_API = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"


METRICS = {
    "Google SEO score": "up",
    "Google accessibility score": "up",
    "Google performance score": "up",
    "Google best practices score": "up",
    "Page weight (KB)": "down",
    "Largest contentful paint (ms)": "down",
    "Observed image transfer (KB)": "down",
    "Oversized images": "down",
    "Below-fold images without lazy loading": "down",
    "Rule checks passed": "up",
    "High-priority issues": "down",
    "Images with alt text (%)": "up",
    "Title length (characters)": None,
    "Meta description length (characters)": None,
    "Word count": None,
}


def find_performance_issues(images):
    """Turn the measured images into a list of planned optimizations."""
    images = [i for i in images if i["url"] and not i["url"].startswith("data:") and i["shown_width"] > 0]
    above_fold = [i for i in images if i["top"] < audit.FOLD and i["shown_width"] >= 300]
    hero = max(above_fold, key=lambda i: i["shown_width"], default=None)

    items = []
    for image in images:
        needed_width = max(i["shown_width"] for i in images if i["url"] == image["url"]) * 2
        item = {
            "src": image["src"], "url": image["url"], "bytes": image["bytes"], "notes": [],
            "needed_width": needed_width,
            "oversized": image["natural_width"] > needed_width * 1.1,
            "heavy": image["bytes"] > HEAVY_IMAGE_BYTES,
            "lazy": image["top"] >= audit.FOLD and image["loading"] != "lazy",
            "priority": image is hero and not image["fetchpriority"] and image["loading"] != "lazy",
        }
        if item["oversized"]:
            item["notes"].append(f"{image['natural_width']}px wide; sampled layouts show it up to {image['shown_width']}px")
        if item["heavy"]:
            item["notes"].append(f"{image['bytes'] // 1024} KB file")
        if item["lazy"]:
            item["notes"].append("below the fold but loads immediately")
        if item["priority"]:
            item["notes"].append("main image at the top; should download first")
        if item["notes"]:
            items.append(item)
    return items


def google_scores(url):
    if not settings.PAGESPEED_API_KEY:
        return {}
    params = [("url", url), ("strategy", "mobile")]
    params += [("category", c) for c in ("SEO", "ACCESSIBILITY", "PERFORMANCE", "BEST_PRACTICES")]
    if settings.PAGESPEED_API_KEY:
        params.append(("key", settings.PAGESPEED_API_KEY))
    try:
        response = requests.get(PAGESPEED_API, params=params, timeout=30)
        response.raise_for_status()
        lighthouse = response.json()["lighthouseResult"]
    except (requests.RequestException, KeyError, ValueError):
        return {}  # Google scores show as "n/a" instead of breaking the audit
    scores = {name: round(cat["score"] * 100) for name, cat in lighthouse["categories"].items()
              if cat.get("score") is not None}
    audits = lighthouse.get("audits", {})
    weight = audits.get("total-byte-weight", {}).get("numericValue")
    lcp = audits.get("largest-contentful-paint", {}).get("numericValue")
    if weight is not None:
        scores["page_weight_kb"] = round(weight / 1024)
    if lcp is not None:
        scores["lcp_ms"] = round(lcp)
    return scores


def measure(url, trusted_origin=None):
    """Read the page and collect every number for the comparison table."""
    with ThreadPoolExecutor(max_workers=1) as pool:
        # Google runs while we read the page; it cannot reach the CLI's local preview.
        google_job = pool.submit(google_scores, url) if not trusted_origin else None
        page = audit.fetch_page(url, trusted_origin)
        page_data = audit.extract_page_data(page["html"], page["url"])
        page_data.update(audit.read_robots(page["url"]))
        issues = audit.run_checks(page_data)
        perf_items = find_performance_issues(page["images"])
        google = google_job.result() if google_job else {}

    images = page_data["image_count"]
    image_bytes = {i["url"]: i["bytes"] for i in page["images"]}
    metrics = {
        "Google SEO score": google.get("seo"),
        "Google accessibility score": google.get("accessibility"),
        "Google performance score": google.get("performance"),
        "Google best practices score": google.get("best-practices"),
        "Page weight (KB)": google.get("page_weight_kb"),
        "Largest contentful paint (ms)": google.get("lcp_ms"),
        "Observed image transfer (KB)": round(sum(image_bytes.values()) / 1024),
        "Oversized images": sum(1 for i in perf_items if i["oversized"]),
        "Below-fold images without lazy loading": sum(1 for i in perf_items if i["lazy"]),
        "Rule checks passed": audit.TOTAL_CHECKS - len(issues),
        "High-priority issues": sum(1 for i in issues if i["severity"] == "high"),
        "Images with alt text (%)": round(100 * (images - page_data["images_missing_alt_count"]) / images) if images else 100,
        "Title length (characters)": len(page_data["title"] or ""),
        "Meta description length (characters)": len(page_data["meta_description"] or ""),
        "Word count": page_data["word_count"],
    }
    return page_data, issues, metrics, perf_items, page["screenshot"]


def save_screenshot(png):
    settings.SCREENSHOT_DIR.mkdir(exist_ok=True)
    shot_id = uuid4().hex
    (settings.SCREENSHOT_DIR / f"{shot_id}.png").write_bytes(png)
    return shot_id


def visual_difference(shot_id, after_png):
    """Compare two full-page screenshots and paint the changed pixels red."""
    from PIL import Image, ImageChops
    if not re.fullmatch(r"[0-9a-f]{32}", shot_id) or not (settings.SCREENSHOT_DIR / f"{shot_id}.png").exists():
        return None
    before = Image.open(settings.SCREENSHOT_DIR / f"{shot_id}.png").convert("RGB")
    after = Image.open(io.BytesIO(after_png)).convert("RGB")
    width, height = max(before.width, after.width), max(before.height, after.height)
    old_size = before.size
    old_canvas = Image.new("RGB", (width, height), "white")
    after_crop = Image.new("RGB", (width, height), "white")
    old_canvas.paste(before, (0, 0))
    after_crop.paste(after, (0, 0))
    before = old_canvas

    # A pixel counts as changed if its colour moved noticeably (ignores tiny
    # differences from image re-compression).
    changed = ImageChops.difference(before, after_crop).convert("L").point(lambda v: 255 if v > 24 else 0)
    percent = round(100 * changed.histogram()[255] / (width * height), 2)

    picture = Image.blend(after_crop, Image.new("RGB", after_crop.size, "white"), 0.65)
    picture.paste(Image.new("RGB", picture.size, (220, 30, 30)), mask=changed)
    picture.thumbnail((720, 2400))
    buffer = io.BytesIO()
    picture.save(buffer, "JPEG", quality=80)
    return {"percent": percent, "height_change": after.height - old_size[1],
            "image": base64.b64encode(buffer.getvalue()).decode()}


def compare(before, after=None):
    """Build the table rows: metric, before, after, change, and a verdict."""
    rows = []
    for name, good_direction in METRICS.items():
        b = before.get(name)
        a = after.get(name) if after else None
        row = {"name": name, "before": b, "after": a, "change": "", "verdict": ""}
        if b is not None and a is not None:
            diff = a - b
            row["change"] = f"{diff:+d}" if diff else "no change"
            if diff and good_direction:
                improved = diff > 0 if good_direction == "up" else diff < 0
                row["verdict"] = "better" if improved else "worse"
        rows.append(row)
    return rows
