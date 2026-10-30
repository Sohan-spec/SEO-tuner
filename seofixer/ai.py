"""Build and search the Chroma knowledge base, then run four parallel agents.

Read in this order: ingest -> search_guidelines -> run_agent -> run_all_agents.
The prompts and tool definitions are in prompts.py."""

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache

import requests
from bs4 import BeautifulSoup
from django.conf import settings

from . import prompts


@lru_cache(maxsize=1)
def get_collection():
    """Chroma runs locally and uses its built-in MiniLM embedding model."""
    import chromadb
    client = chromadb.PersistentClient(path=str(settings.CHROMA_DIR))
    return client.get_or_create_collection(
        "seo_guidelines_cosine", configuration={"hnsw": {"space": "cosine"}}
    )


def chunk_text(text, size=130, overlap=30):
    """Split into short word chunks with a little shared context."""
    if not 0 <= overlap < size:
        raise ValueError("Chunk overlap must be smaller than chunk size.")
    words = text.split()
    chunks = []
    for start in range(0, len(words), size - overlap):
        chunks.append(" ".join(words[start:start + size]))
        if start + size >= len(words):
            break
    return chunks


def ingest():
    """Refresh Google's guidance. Failed downloads keep the previous version."""
    collection = get_collection()
    stored, failures = 0, []
    for url in settings.SEO_SOURCES:
        try:
            response = requests.get(url, timeout=20)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, "html.parser")
            main = soup.select_one(".devsite-article-body") or soup.find("article") or soup.find("main")
            if main is None:
                raise ValueError("Article content was not found.")
            for tag in main(["script", "style", "nav", "footer", "aside"]):
                tag.decompose()
            chunks = chunk_text(main.get_text(" ", strip=True))
            if not chunks:
                raise ValueError("Article was empty.")
            ids = [f"{url}#{i}" for i in range(len(chunks))]
            for start in range(0, len(chunks), 100):
                collection.upsert(
                    ids=ids[start:start+100], documents=chunks[start:start+100],
                    metadatas=[{"source": url} for _ in chunks[start:start+100]],
                )
            old_ids = collection.get(where={"source": url})["ids"]
            stale = list(set(old_ids) - set(ids))
            if stale:
                collection.delete(ids=stale)
            stored += 1
        except (requests.RequestException, ValueError) as error:
            failures.append(f"{url}: {error}")
    return {"stored": stored, "failures": failures}


def search_guidelines(query):
    """Retrieve only by cosine similarity, including each passage's source."""
    collection = get_collection()
    if not collection.count():
        return []
    results = collection.query(query_texts=[query], n_results=min(settings.SEARCH_RESULTS, collection.count()))
    return [{"source": meta["source"], "text": text, "score": round(1 - distance, 2)}  # cosine similarity
            for text, meta, distance in zip(results["documents"][0], results["metadatas"][0], results["distances"][0])]


UNSAFE = re.compile(r"<script|<iframe|<style|javascript:|\son\w+\s*=", re.I)
HEAD_TAG = re.compile(r'</title>\s*<meta name="(description|viewport)" content="[^"<>]*"\s*/?>')


def adds_missing_head_tag(replace, page_data):
    """After </title> only a meta description or viewport tag that the page lacks may be added."""
    tag = HEAD_TAG.fullmatch(replace)
    return bool(tag) and not (page_data["meta_description"] if tag[1] == "description" else page_data["has_viewport"])


def validate_report(report, sources, page_data):
    if not isinstance(report, dict) or not isinstance(report.get("summary"), str):
        raise ValueError("The agent returned an invalid report.")
    score, fixes = report.get("score"), report.get("fixes")
    if type(score) is not int or not 0 <= score <= 100 or not isinstance(fixes, list):
        raise ValueError("The agent returned an invalid score or fixes.")
    clean = []
    for fix in fixes[:4]:
        fields = ("issue", "severity", "why", "source", "find", "replace")
        # Drop a malformed fix instead of losing the agent's whole report.
        if (not isinstance(fix, dict) or any(not isinstance(fix.get(k), str) for k in fields)
                or fix["severity"] not in ("high", "medium", "low")
                or any(len(fix[k]) > 4000 for k in fields)):
            continue
        fix = {key: fix[key] for key in fields}
        if fix["source"] not in sources:
            fix["source"] = ""
            fix["find"] = fix["replace"] = ""
        # Only reviewed, page-derived text may become an automatic edit.
        # Body text needs 20+ characters: a short phrase could match an unrelated spot in the repo.
        find = fix["find"]
        known = (len(find) >= 20 and find in page_data["body_text"]) or find in (page_data["title"], page_data["meta_description"])
        known = known or any(find == text for level, text in page_data["headings"])
        known = known or (find == "</title>" and bool(page_data["title"]) and adds_missing_head_tag(fix["replace"], page_data))
        known = known or any(find in (f'src="{src}"', f"src='{src}'")
                             for src in page_data["images_missing_alt"])
        # The new text goes into the repo, so it may never carry code (the audited page could ask for it).
        if not known or UNSAFE.search(fix["replace"]):
            fix["find"] = fix["replace"] = ""
        elif find == "</title>":  # every page has </title>; anchor the insert to this page's title
            fix["find"], fix["replace"] = page_data["title"] + find, page_data["title"] + fix["replace"]
        clean.append(fix)
    return {"score": score, "summary": report["summary"][:2000], "fixes": clean}


def run_agent(specialist, page_data, issues, on_event=None):
    """Each specialist searches guidance, then submits a validated report.

    on_event(agent, status, **details) reports progress to the CLI; the web app leaves it out."""
    import anthropic
    notify = lambda status, **details: on_event and on_event(specialist["key"], status, **details)
    notify("thinking")
    client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY, timeout=120, max_retries=1)
    page_slice = {field: page_data[field] for field in specialist["fields"]}
    if specialist["body_chars"]:
        page_slice["body_text"] = page_data["body_text"][:specialist["body_chars"]]
    my_issues = [issue for issue in issues if issue["area"] == specialist["key"]]
    system_prompt = prompts.SYSTEM_PROMPT.format(name=specialist["name"], focus=specialist["focus"])
    messages = [{"role": "user", "content": json.dumps({"page": page_slice, "issues": my_issues})}]
    usage = {"input_tokens": 0, "output_tokens": 0, "turns": 0}
    sources, searches = set(), 0
    tools = prompts.TOOLS + [prompts.WEB_SEARCH_TOOL]
    turn = resumes = 0

    while turn < settings.AGENT_MAX_TURNS:
        # No forced tool_choice: Opus 5.5 rejects it, so the prompt and tool results steer the order.
        try:
            response = client.messages.create(
                model=settings.LLM_MODEL, max_tokens=settings.LLM_MAX_TOKENS,
                system=system_prompt, tools=tools, messages=messages,
            )
        except (anthropic.BadRequestError, anthropic.PermissionDeniedError):
            if prompts.WEB_SEARCH_TOOL not in tools:
                raise
            # The organization may have web search switched off; the knowledge base still works.
            logging.getLogger(__name__).warning("Web search was rejected; retrying without it.", exc_info=True)
            tools = prompts.TOOLS
            continue
        usage["input_tokens"] += response.usage.input_tokens
        usage["output_tokens"] += response.usage.output_tokens
        usage["turns"] += 1
        messages.append({"role": "assistant", "content": response.content})
        for block in response.content:
            # Web results are real URLs, so they become valid citations (https only: they render as links).
            if block.type == "web_search_tool_result" and isinstance(block.content, list):
                sources.update(r.url for r in block.content if r.url.startswith("https://"))
                notify("web", urls=[r.url for r in block.content][:3])
        if response.stop_reason == "pause_turn" and resumes < 2:
            resumes += 1
            continue  # A long web search paused; sending the turn back resumes it.
        turn += 1
        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            if block.name == "submit_report":
                if searches:
                    report = validate_report(block.input, sources, page_data)
                    notify("done", fixes=len(report["fixes"]), score=report["score"])
                    return report, usage
                tool_results.append({"type": "tool_result", "tool_use_id": block.id, "is_error": True,
                                     "content": "Search with search_seo_guidelines before submitting."})
                continue
            query = block.input.get("query")
            if block.name != "search_seo_guidelines" or not isinstance(query, str) or not query.strip():
                # Let the agent correct itself instead of failing the whole report.
                tool_results.append({"type": "tool_result", "tool_use_id": block.id, "is_error": True,
                                     "content": "Unknown tool or empty query."})
                continue
            passages = search_guidelines(query[:500]) if searches < 3 else []
            searches += 1
            sources.update(p["source"] for p in passages)
            notify("search", query=query[:80], results=[(p.get("score"), p["source"]) for p in passages])
            tool_results.append({"type": "tool_result", "tool_use_id": block.id,
                                 "content": json.dumps(passages)})
        if not tool_results or turn == settings.AGENT_MAX_TURNS - 1:  # next turn is the last one
            tool_results.append({"type": "text", "text": prompts.REPORT_REMINDER})
        messages.append({"role": "user", "content": tool_results})
    raise ValueError("The agent did not finish its report.")


SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}


def run_all_agents(page_data, issues, on_event=None):
    """Run every specialist in parallel, then merge their reports in Python."""
    empty = {"score": None, "areas": [], "fixes": [], "failures": []}
    usage = {"input_tokens": 0, "output_tokens": 0, "turns": 0}
    if not settings.ANTHROPIC_API_KEY:
        empty["failures"] = ["AI suggestions are off. Add ANTHROPIC_API_KEY to .env and restart."]
        return empty, usage
    if not get_collection().count():
        empty["failures"] = ["Build the SEO knowledge base before running AI suggestions."]
        return empty, usage
    with ThreadPoolExecutor(max_workers=len(prompts.SPECIALISTS)) as pool:
        jobs = [(s, pool.submit(run_agent, s, page_data, issues, on_event)) for s in prompts.SPECIALISTS]

    areas, all_fixes, failures = [], [], []
    usage = {"input_tokens": 0, "output_tokens": 0, "turns": 0}
    for specialist, job in jobs:
        try:
            report, agent_usage = job.result()
        except Exception:  # Keep the reports from specialists that succeeded.
            logging.getLogger(__name__).exception("Specialist failed")
            failures.append(f"{specialist['name']} could not finish. Check the key, quota, and terminal log.")
            if on_event:
                on_event(specialist["key"], "failed")
            continue
        areas.append({"name": specialist["name"], "score": report["score"], "summary": report["summary"]})
        for fix in report["fixes"]:
            fix["agent"] = specialist["name"]
            all_fixes.append(fix)
        for key in usage:
            usage[key] += agent_usage[key]

    if not areas:
        empty["failures"] = failures
        return empty, usage

    # Merge: most severe first, drop duplicates that target the same text.
    all_fixes.sort(key=lambda fix: SEVERITY_ORDER.get(fix["severity"], 3))
    fixes, seen = [], set()
    for fix in all_fixes:
        key = fix["find"].strip() or fix["issue"].strip().lower()
        if key not in seen:
            seen.add(key)
            fixes.append(fix)

    combined = {
        "score": round(sum(a["score"] for a in areas) / len(areas)),
        "areas": areas,
        "fixes": fixes[:settings.MAX_FIXES],
        "failures": failures,
    }
    return combined, usage
