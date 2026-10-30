"""Start here: handle the form, call the right module, and render the page.

audit.py reads the website; measurements.py collects numbers and screenshots.
ai.py runs the specialists; github.py prepares the selected changes.
The session stores the audit so submitted forms cannot replace its contents.
"""
import logging
from uuid import uuid4

from django.conf import settings
from django.shortcuts import redirect, render
from django.views.decorators.http import require_http_methods

from . import ai, audit, github, measurements


def start_audit(url, repo):
    """Read the page, run the agents, and collect everything the page displays."""
    url = audit.validate_url(url.strip())
    repo = github.parse_repo(repo.strip()) if repo.strip() else ""
    page, issues, before, images, screenshot = measurements.measure(url)
    report, usage = ai.run_all_agents(page, issues)
    return {
        "audit_id": uuid4().hex,
        "url": page["url"],
        "repo": repo,
        "issues": issues,
        "before": before,
        "perf_items": images,
        "report": report,
        "usage": usage,
        "shot_id": measurements.save_screenshot(screenshot),
    }


def open_selected_pr(state, selection, include_images):
    """Use only the saved audit and the checkboxes selected by the user."""
    if not settings.GITHUB_TOKEN:
        raise ValueError("Add GITHUB_TOKEN to .env and restart to create draft PRs.")
    if not state["repo"]:
        raise ValueError("Audit again with the site's GitHub repository.")
    if state.get("pr"):
        return state["pr"]

    all_fixes = state["report"]["fixes"]
    selected = {int(value) for value in selection}
    if any(index < 0 or index >= len(all_fixes) for index in selected):
        raise ValueError("Invalid fix selection.")
    fixes = [fix for index, fix in enumerate(all_fixes) if index in selected]
    images = state["perf_items"] if include_images else []
    if not fixes and not images:
        raise ValueError("Select at least one change.")
    return github.create_pull_request(state["repo"], fixes, state["url"], images)


@require_http_methods(["GET", "POST"])
def home(request):
    state = request.session.get("audit", {})
    notice = request.session.pop("notice", "")
    error = ""

    if request.method == "POST":
        action = request.POST.get("action")
        try:
            if action in ("pr", "measure"):
                if not state or request.POST.get("audit_id") != state["audit_id"]:
                    raise ValueError("This audit is missing or outdated. Run a fresh audit.")

            if action == "ingest":
                result = ai.ingest()
                notice = f"Refreshed {result['stored']} sources. {len(result['failures'])} downloads failed."
                if result["failures"]:
                    logging.getLogger(__name__).warning("Guidance downloads: %s", result["failures"])
            elif action == "audit":
                state = start_audit(request.POST.get("url", ""), request.POST.get("repo", ""))
            elif action == "pr":
                state["pr"] = open_selected_pr(
                    state, request.POST.getlist("selected"), bool(request.POST.get("perf"))
                )
            elif action == "measure":
                url = request.POST.get("after_url", "").strip() or state["url"]
                audit.validate_url(url)
                _, _, after, _, screenshot = measurements.measure(url)
                state["after"] = after
                state["visual"] = measurements.visual_difference(state["shot_id"], screenshot)
            else:
                raise ValueError("Unknown action.")

            request.session["audit"] = state
            request.session["notice"] = notice
            return redirect("home")
        except ValueError as problem:
            error = str(problem)
        except Exception:
            logging.getLogger(__name__).exception("SEO operation failed")
            error = "The operation could not finish. Check the connection, setup, and terminal log; then retry."

    context = {**state, "error": error, "notice": notice}
    if state:
        context["rows"] = measurements.compare(state["before"], state.get("after"))
    return render(request, "seofixer/index.html", context)
