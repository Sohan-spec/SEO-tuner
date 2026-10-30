"""Backend for the seofixer TUI: runs one action and prints one JSON event per line.

    python -m seofixer.cli <action> --project DIR --url http://127.0.0.1:PORT/

The TUI (cli/seofixer.js) serves DIR at --url, starts this, and draws the events.
"""
import argparse
import json
import logging
import os
import re
import sys
import threading
import time
from pathlib import Path

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "seofixer.settings")
import django  # noqa: E402

django.setup()

from django.conf import settings  # noqa: E402

from . import ai, audit, local, measurements, prompts  # noqa: E402

_lock = threading.Lock()


def emit(event, **data):
    with _lock:  # the four agents report from parallel threads
        print(json.dumps({"event": event, **data}), flush=True)


def step(label, fn, *args):
    emit("step", label=label, status="running")
    result = fn(*args)
    emit("step", label=label, status="done")
    return result


def measure(args, label="Rendering your page in a headless browser"):
    page_data, issues, metrics, perf_items, _ = step(label, measurements.measure, args.url, args.url)
    return page_data, issues, metrics, perf_items


def visible_rows(before, after=None):
    return [r for r in measurements.compare(before, after) if r["before"] is not None or r["after"] is not None]


def baseline_file(args):
    folder = Path(args.project) / ".seofixer"
    folder.mkdir(exist_ok=True)
    return folder / "baseline.json"


def require_ai():
    if not settings.ANTHROPIC_API_KEY or not ai.get_collection().count():
        raise ValueError("AI is off: add ANTHROPIC_API_KEY to .env and choose \"Refresh Google's guidance\" once.")


def report_agent(agent, status, **details):
    emit("agent", agent=agent, status=status, **details)


def advice(fixes):
    keep = ("severity", "issue", "why", "source", "agent")
    emit("advice", fixes=[{k: f.get(k, "") for k in keep} | {"auto": bool(f["find"])} for f in fixes])


def run_agents(page_data, issues):
    require_ai()
    report, usage = ai.run_all_agents(page_data, issues, report_agent)
    for failure in report["failures"]:
        emit("warning", message=failure)
    advice(report["fixes"])
    emit("usage", **usage)
    return report


def line_of(path, text):
    """Line where `text` ends in the file, so the editor lands on the new content."""
    content = path.read_text("utf-8")
    start = content.find(text.strip())
    return content[:start + len(text.strip())].count("\n") + 1 if start >= 0 else 1


def mark_for(replace):
    """What the preview should scroll to: an image src, a meta content, or the visible text."""
    match = re.search(r'(?:src|content)="([^"]+)"', replace)
    return match[1] if match else re.sub(r"<[^>]+>", "", replace).strip()


def apply_and_show(args, fixes, perf_items):
    root = Path(args.project)
    result = step("Applying the safe changes to your files", local.apply_to_folder, args.project, fixes, args.url, perf_items)
    for fix, path in result["applied"]:
        emit("change", label=fix["issue"], file=path, line=line_of(root / path, fix["replace"]),
             mark=mark_for(fix["replace"]), source=fix["source"])
    for label, path in result["attributes"]:
        name, src = label.split(" on ", 1)
        emit("change", label="Added " + name, file=path, line=line_of(root / path, src), mark=src, source="")
    for image in result["images"]:
        emit("change", label=f"Compressed {image['old_kb']:,} KB → {image['new_kb']:,} KB", file=image["path"],
             line=None, mark=None, source="")
    for fix, reason in result["skipped"]:
        if fix.get("find"):  # advice-only fixes were already listed
            emit("skipped", label=fix["issue"], reason=reason)


# ---------------- actions (one per menu option) ----------------

def check(args):
    page_data, issues, metrics, _ = measure(args)
    emit("issues", items=issues)
    emit("metrics", rows=visible_rows(metrics))
    baseline_file(args).write_text(json.dumps(metrics))
    run_agents(page_data, issues)


def fix(args):
    page_data, issues, before, perf_items = measure(args)
    emit("issues", items=issues)
    report = run_agents(page_data, issues)
    apply_and_show(args, report["fixes"], perf_items)
    baseline_file(args).write_text(json.dumps(before))
    _, issues, after, _ = measure(args, "Measuring your page again")
    emit("metrics", rows=visible_rows(before, after))


def images(args):
    _, _, before, perf_items = measure(args)
    apply_and_show(args, [], perf_items)
    baseline_file(args).write_text(json.dumps(before))
    _, _, after, _ = measure(args, "Measuring your page again")
    emit("metrics", rows=visible_rows(before, after))


def visibility(args):
    data, _, _, _ = measure(args)
    robots = re.split(r"[\s,]+", (data["meta_robots"] or "").lower())
    facts = [
        ("noindex" not in robots, "Google is allowed to index this page"),
        ("nosnippet" not in robots and "max-snippet:0" not in robots and not data["data_nosnippet"],
         "Google and its AI answers may quote this page (no snippet limits)"),
        (bool(data["title"]) and bool(data["meta_description"]), "The page has a title and a meta description"),
    ]
    if "blocked_search_bots" in data:
        for bot in audit.SEARCH_BOTS:
            facts.append((bot not in data["blocked_search_bots"], f"{bot} (search) can crawl this page"))
        if data["blocked_training_bots"]:
            facts.append((None, "Blocks AI training only: " + ", ".join(data["blocked_training_bots"]) + " (search is not affected)"))
        facts.append((data["sitemap_listed"], "robots.txt lists a sitemap"))
    else:
        facts.append((None, "Could not read robots.txt"))
    facts += [(None, "Tip: in Search Console, keep \"Search generative AI\" on Include (the default)."),
              (None, "Tip: a verified, complete Google Business Profile helps local and AI results.")]
    emit("facts", items=[{"ok": ok, "text": text} for ok, text in facts])


def ideas(args):
    require_ai()
    page_data, issues, _, _ = measure(args)
    content = next(s for s in prompts.SPECIALISTS if s["key"] == "content")
    report, _ = ai.run_agent(content, page_data, issues, report_agent)
    ideas = [f for f in report["fixes"] if "idea" in f["issue"].lower()]
    if ideas:
        emit("ideas", text=ideas[0]["why"], source=ideas[0]["source"])
    advice([f for f in report["fixes"] if f not in ideas])


def measure_again(args):
    _, issues, after, _ = measure(args)
    emit("issues", items=issues)
    path = baseline_file(args)
    if path.exists():
        emit("metrics", rows=visible_rows(json.loads(path.read_text()), after))
    else:
        emit("warning", message="No earlier check to compare with yet, so these are today's numbers.")
        emit("metrics", rows=visible_rows(after))


def undo(args):
    restored = local.undo(args.project)
    emit("restored", files=restored)


def refresh(args):
    result = step("Downloading Google's latest guidance", ai.ingest)
    emit("refreshed", stored=result["stored"], total=len(settings.SEO_SOURCES), failures=result["failures"])


ACTIONS = {"check": check, "fix": fix, "images": images, "visibility": visibility,
           "ideas": ideas, "measure": measure_again, "undo": undo, "refresh": refresh}


def main():
    parser = argparse.ArgumentParser(description="seofixer backend for the TUI")
    parser.add_argument("action", choices=ACTIONS)
    parser.add_argument("--project", default=".", help="the website folder (with index.html)")
    parser.add_argument("--url", help="the TUI's local preview of that folder")
    args = parser.parse_args()
    started = time.time()
    try:
        ACTIONS[args.action](args)
    except Exception as error:  # the TUI shows a short message; details go to stderr
        logging.getLogger(__name__).exception("seofixer %s failed", args.action)
        emit("error", message=str(error) or type(error).__name__)
        sys.exit(1)
    emit("done", seconds=round(time.time() - started, 1))


if __name__ == "__main__":
    main()
