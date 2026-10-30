"""Render demo.html frame by frame into a 45-second MP4.

Run: .venv/bin/python demo_video/render.py [--fps 60] [--still 12.5 ...]
Each frame calls render(t) with an exact time, so the video is in sync no matter how slow a frame is.
"""
import argparse
import shutil
import subprocess
from multiprocessing import Pool
from pathlib import Path

from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
PAGE = (HERE / "demo.html").as_uri() + "?capture=1"
DURATION = 45


def render_frames(job):
    frames, fps, out_dir = job
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1920, "height": 1080})
        page.goto(PAGE)
        page.evaluate("document.fonts.ready")
        for i in frames:
            page.evaluate("t => render(t)", i / fps)
            page.screenshot(path=str(out_dir / f"{i:05d}.jpg"), type="jpeg", quality=93)
        browser.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--fps", type=int, default=60)
    parser.add_argument("--workers", type=int, default=7)
    parser.add_argument("--still", type=float, nargs="*", help="render PNG stills at these seconds instead")
    args = parser.parse_args()

    if args.still:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport={"width": 1920, "height": 1080})
            page.goto(PAGE)
            for t in args.still:
                page.evaluate("t => render(t)", t)
                page.screenshot(path=str(HERE / f"still_{t:05.2f}.png"))
            browser.close()
        return

    out_dir = HERE / "frames"
    shutil.rmtree(out_dir, ignore_errors=True)
    out_dir.mkdir()
    total = DURATION * args.fps
    jobs = [(range(w, total, args.workers), args.fps, out_dir) for w in range(args.workers)]
    with Pool(args.workers) as pool:
        pool.map(render_frames, jobs)
    assert len(list(out_dir.glob("*.jpg"))) == total, "missing frames"

    video = HERE / "seofixer_demo.mp4"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-framerate", str(args.fps), "-i", str(out_dir / "%05d.jpg"),
                    "-c:v", "libx264", "-preset", "slow", "-crf", "16", "-pix_fmt", "yuv420p",
                    "-movflags", "+faststart", str(video)], check=True)
    shutil.rmtree(out_dir)
    print(video)


if __name__ == "__main__":
    main()
