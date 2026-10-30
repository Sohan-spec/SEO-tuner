"""Prepare reviewed text and image changes and open one draft GitHub PR.

create_pull_request is the entry point. The smaller helpers above it handle
image compression, file matching, and GitHub requests. Nothing is merged here."""

import base64
import io
import re
from urllib.parse import quote, unquote, urlparse
from uuid import uuid4

import requests
from bs4 import BeautifulSoup
from django.conf import settings


GITHUB_API = "https://api.github.com"


EDITABLE_EXTENSIONS = (".html", ".htm", ".jsx", ".tsx", ".js", ".ts", ".vue",
                       ".svelte", ".astro", ".md", ".mdx", ".php", ".erb")


SKIPPED_FOLDERS = ("node_modules/", "dist/", "build/", ".next/", "vendor/")


IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp")


def optimize_image(data, needed_width=None):
    """Return a smaller version of the image in the same format, or None."""
    from PIL import Image, ImageOps
    image = Image.open(io.BytesIO(data))
    image_format = image.format
    if image_format not in ("JPEG", "PNG", "WEBP") or getattr(image, "is_animated", False) or image.mode == "CMYK":
        return None
    color_profile = image.info.get("icc_profile")
    if image_format == "JPEG":
        image = ImageOps.exif_transpose(image)  # keep the photo's rotation

    if needed_width is not None and (type(needed_width) is not int or needed_width < 1):
        raise ValueError("Image width must be a positive integer.")
    if needed_width and image.width > needed_width * 1.1:
        if image.mode == "P":
            image = image.convert("RGBA")
        new_height = max(1, round(image.height * needed_width / image.width))
        image = image.resize((needed_width, new_height), Image.LANCZOS)

    out = io.BytesIO()
    if image_format == "JPEG":
        image.convert("RGB").save(out, "JPEG", quality=82, optimize=True, progressive=True, icc_profile=color_profile)
    elif image_format == "PNG":
        image.save(out, "PNG", optimize=True, icc_profile=color_profile)
    else:
        image.save(out, "WEBP", quality=82, method=6, icc_profile=color_profile)

    smaller = out.getvalue()
    return smaller if len(smaller) < len(data) * 0.9 else None


def repo_path_for(image_url, repo_paths):
    """Match an image URL like /myrepo/img/hero.jpg to the file img/hero.jpg."""
    url_path = unquote(urlparse(image_url).path)
    exact = [p for p in repo_paths if url_path.endswith("/" + p)]
    if exact:
        return max(exact, key=len)
    nested = [p for p in repo_paths if ("/" + p).endswith(url_path)]  # e.g. public/img/hero.jpg
    return nested[0] if len(nested) == 1 else None


def github(method, api_path, **kwargs):
    response = requests.request(
        method, GITHUB_API + api_path, timeout=30, allow_redirects=False,
        headers={"Authorization": f"Bearer {settings.GITHUB_TOKEN}",
                 "Accept": "application/vnd.github+json"},
        **kwargs,
    )
    response.raise_for_status()
    return response.json()


def parse_repo(text):
    """Accept exactly owner/repo, optionally as a GitHub HTTPS URL."""
    text = text.strip().rstrip("/")
    if text.startswith("https://github.com/"):
        text = text[len("https://github.com/"):]
    text = text.removesuffix(".git")
    if not re.fullmatch(r"[A-Za-z0-9-]+/[A-Za-z0-9_.-]+", text) or text.split("/")[-1] in (".", ".."):
        raise ValueError("Enter a GitHub repository as owner/repo.")
    return text


def replace_once_in_repo(files, find, replace):
    """Apply only an exact match that occurs once across all inspected files."""
    if not find or find == replace:
        return None
    matches = [path for path, file in files.items() if find in file["text"]]
    if len(matches) != 1 or files[matches[0]]["text"].count(find) != 1:
        return None
    file = files[matches[0]]
    file["text"] = file["text"].replace(find, replace, 1)
    file["changed"] = True
    return matches[0]


def add_image_attribute(files, src, name, value):
    """Only literal HTML image tags; JSX and template expressions need review."""
    matches = []
    for path, file in files.items():
        if not path.endswith((".html", ".htm")):
            continue
        for match in re.finditer(r"<img\b[^>]*>", file["text"], flags=re.I):
            tag = BeautifulSoup(match.group(), "html.parser").img
            if tag.get("src") == src:
                matches.append((path, match, tag))
    if len(matches) != 1 or matches[0][2].get(name) == value:
        return None
    path, match, tag = matches[0]
    tag[name] = value
    files[path]["text"] = files[path]["text"][:match.start()] + str(tag) + files[path]["text"][match.end():]
    files[path]["changed"] = True
    return path


def create_pull_request(repo, fixes, page_url, perf_items):
    repo = parse_repo(repo)
    base_branch = github("GET", f"/repos/{repo}")["default_branch"]
    base_sha = github("GET", f"/repos/{repo}/git/ref/heads/{quote(base_branch, safe='')}")["object"]["sha"]
    tree = github("GET", f"/repos/{repo}/git/trees/{base_sha}", params={"recursive": "1"})
    if tree.get("truncated"):
        raise ValueError("Repository listing is incomplete; automatic edits are disabled.")
    blobs = {item["path"]: item for item in tree["tree"] if item["type"] == "blob" and item["mode"] in ("100644", "100755")
             and not any(folder in item["path"] for folder in SKIPPED_FOLDERS)}

    def download(file_path):
        return base64.b64decode(github("GET", f"/repos/{repo}/git/blobs/{blobs[file_path]['sha']}")["content"])

    # Download the text files that could contain page content.
    files = {}
    candidates = [p for p in blobs if p.endswith(EDITABLE_EXTENSIONS)]
    if len(candidates) > 150 or any(blobs[p].get("size", 0) > 200_000 for p in candidates):
        raise ValueError("Repository is too large for this demo's complete text check.")
    for file_path in candidates:
        files[file_path] = {"text": download(file_path).decode("utf-8"), "changed": False}

    result = {"pr_url": None, "applied": [], "skipped": [], "images": [], "attributes": []}

    # Step 1: the AI's SEO text fixes.
    for fix in fixes:
        if not fix.get("find"):
            result["skipped"].append((fix, "Needs a manual change."))
            continue
        file_path = replace_once_in_repo(files, fix["find"], fix["replace"])
        if file_path:
            result["applied"].append((fix, file_path))
        else:
            result["skipped"].append((fix, "Text not found exactly once in the repo."))

    # Step 2: performance attributes and image files.
    image_paths = [p for p in blobs if p.lower().endswith(IMAGE_EXTENSIONS) and blobs[p].get("size", 0) < 25_000_000]
    new_images = {}
    for item in perf_items:
        if urlparse(item["url"]).netloc != urlparse(page_url).netloc:
            continue  # A third-party image is not necessarily an asset in this repo.
        for flag, name, value in (("lazy", "loading", "lazy"), ("priority", "fetchpriority", "high")):
            if item[flag] and item["src"]:
                file_path = add_image_attribute(files, item["src"], name, value)
                if file_path:
                    result["attributes"].append(f'{name}="{value}" on {item["src"]} in {file_path}')
        if item["oversized"] or item["heavy"]:
            file_path = repo_path_for(item["url"], image_paths)
            if file_path and file_path not in new_images:
                original = download(file_path)
                # Resize only an image this page alone uses; another page may show it larger.
                used_once = sum(f["text"].count(file_path.rsplit("/", 1)[-1]) for f in files.values()) == 1
                smaller = optimize_image(original, item["needed_width"] if item["oversized"] and used_once else None)
                if smaller:
                    new_images[file_path] = smaller
                    result["images"].append({"path": file_path, "old_kb": len(original) // 1024,
                                             "new_kb": len(smaller) // 1024})

    changed = {p: f["text"].encode("utf-8") for p, f in files.items() if f["changed"]}
    changed.update(new_images)
    if not changed:
        return result

    # Step 3: one commit with every change, on a new branch, then the PR.
    entries = []
    for file_path, data in changed.items():
        blob = github("POST", f"/repos/{repo}/git/blobs", json={
            "content": base64.b64encode(data).decode(), "encoding": "base64",
        })
        entries.append({"path": file_path, "mode": blobs[file_path]["mode"],
                        "type": "blob", "sha": blob["sha"]})
    new_tree = github("POST", f"/repos/{repo}/git/trees", json={
        "base_tree": tree["sha"], "tree": entries,
    })
    commit = github("POST", f"/repos/{repo}/git/commits", json={
        "message": "SEO and performance fixes", "tree": new_tree["sha"], "parents": [base_sha]})
    branch = f"seo-fixes-{uuid4().hex[:12]}"
    github("POST", f"/repos/{repo}/git/refs", json={"ref": f"refs/heads/{branch}", "sha": commit["sha"]})

    body = [f"Automated SEO and performance fixes for {page_url}\n"]
    for fix, file_path in result["applied"]:
        body.append(f"### {fix['issue']} ({fix['severity']})\nFile: `{file_path}`\n\n{fix['why']}\n\nSource: {fix['source'] or 'n/a'}\n")
    if result["images"] or result["attributes"]:
        body.append("### Proposed performance changes (visual review required)\n")
        body += [f"- `{i['path']}`: {i['old_kb']} KB to {i['new_kb']} KB" for i in result["images"]]
        body += [f"- Added {a}" for a in result["attributes"]]
    pr = github("POST", f"/repos/{repo}/pulls", json={
        "title": f"SEO and performance fixes ({len(result['applied'])} text, {len(result['images'])} images)",
        "head": branch, "base": base_branch, "body": "\n".join(body), "draft": True,
    })
    result["pr_url"] = pr["html_url"]
    return result
