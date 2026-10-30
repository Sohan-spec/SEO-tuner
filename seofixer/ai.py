"""Build and search the Chroma knowledge base, then run four parallel agents.

Read in this order: ingest -> search_guidelines -> run_agent -> run_all_agents.
The prompts and tool definitions are in prompts.py."""

import json
import logging
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
    return [{"source": meta["source"], "text": text}
            for text, meta in zip(results["documents"][0], results["metadatas"][0])]


def validate_report(report, sources, page_data):
    if not isinstance(report, dict) or not isinstance(report.get("summary"), str):
        raise ValueError("The agent returned an invalid report.")
    score, fixes = report.get("score"), report.get("fixes")
    if type(score) is not int or not 0 <= score <= 100 or not isinstance(fixes, list):
        raise ValueError("The agent returned an invalid score or fixes.")
    clean = []
    for fix in fixes[:4]:
        fields = ("issue", "severity", "why", "source", "find", "replace")
        if not isinstance(fix, dict) or any(not isinstance(fix.get(k), str) for k in fields):
            raise ValueError("The agent returned an incomplete fix.")
        if fix["severity"] not in ("high", "medium", "low"):
            raise ValueError("The agent returned an invalid priority.")
        if any(len(fix[k]) > 4000 for k in fields):
            raise ValueError("The agent returned an oversized fix.")
        fix = {key: fix[key] for key in fields}
        if fix["source"] not in sources:
            fix["source"] = ""
            fix["find"] = fix["replace"] = ""
        # Only reviewed, page-derived text may become an automatic edit.
        find = fix["find"]
        known = find in page_data["body_text"] or find == page_data["title"] or find == page_data["meta_description"]
        known = known or any(find == text for level, text in page_data["headings"])
        known = known or (find == "</title>" and bool(page_data["title"]))
        known = known or any(find in (f'src="{src}"', f"src='{src}'")
                             for src in page_data["images_missing_alt"])
        if not known:
            fix["find"] = fix["replace"] = ""
        clean.append(fix)
    return {"score": score, "summary": report["summary"][:2000], "fixes": clean}


def run_agent(specialist, page_data, issues):
    """Each specialist searches guidance, then submits a validated report."""
    import anthropic
    client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY, timeout=45, max_retries=1)
    page_slice = {field: page_data[field] for field in specialist["fields"]}
    if specialist["body_chars"]:
        page_slice["body_text"] = page_data["body_text"][:specialist["body_chars"]]
    my_issues = [issue for issue in issues if issue["area"] == specialist["key"]]
    system_prompt = prompts.SYSTEM_PROMPT.format(name=specialist["name"], focus=specialist["focus"])
    messages = [{"role": "user", "content": json.dumps({"page": page_slice, "issues": my_issues})}]
    usage = {"input_tokens": 0, "output_tokens": 0, "turns": 0}
    sources, searches = set(), 0

    for turn in range(settings.AGENT_MAX_TURNS):
        # Search first; force completion on the last turn.
        tool_choice = {"type": "auto"}
        if turn == 0:
            tool_choice = {"type": "tool", "name": "search_seo_guidelines"}
        elif turn == settings.AGENT_MAX_TURNS - 1:
            tool_choice = {"type": "tool", "name": "submit_report"}
        response = client.messages.create(
            model=settings.LLM_MODEL, max_tokens=settings.LLM_MAX_TOKENS,
            system=system_prompt, tools=prompts.TOOLS, tool_choice=tool_choice, messages=messages,
        )
        usage["input_tokens"] += response.usage.input_tokens
        usage["output_tokens"] += response.usage.output_tokens
        usage["turns"] += 1
        messages.append({"role": "assistant", "content": response.content})
        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            if block.name == "submit_report":
                if not searches:
                    raise ValueError("The agent did not search the knowledge base.")
                return validate_report(block.input, sources, page_data), usage
            if block.name != "search_seo_guidelines":
                raise ValueError("The agent requested an unknown tool.")
            query = block.input.get("query")
            if not isinstance(query, str) or not query.strip():
                raise ValueError("The agent returned an invalid search query.")
            passages = search_guidelines(query[:500]) if searches < 3 else []
            searches += 1
            sources.update(p["source"] for p in passages)
            tool_results.append({"type": "tool_result", "tool_use_id": block.id,
                                 "content": json.dumps(passages)})
        messages.append({"role": "user", "content": tool_results or prompts.REPORT_REMINDER})
    raise ValueError("The agent did not finish its report.")


SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}


def run_all_agents(page_data, issues):
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
        jobs = [(s, pool.submit(run_agent, s, page_data, issues)) for s in prompts.SPECIALISTS]

    areas, all_fixes, failures = [], [], []
    usage = {"input_tokens": 0, "output_tokens": 0, "turns": 0}
    for specialist, job in jobs:
        try:
            report, agent_usage = job.result()
        except Exception:  # Keep the reports from specialists that succeeded.
            logging.getLogger(__name__).exception("Specialist failed")
            failures.append(f"{specialist['name']} could not finish. Check the key, quota, and terminal log.")
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
