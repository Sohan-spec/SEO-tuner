"""Local SEO Fixer demo. All app settings are here.

Setup:
  python3 -m venv .venv
  source .venv/bin/activate
  pip install -r requirements.txt
  python -m playwright install chromium
  python manage.py migrate
  python manage.py runserver

Run tests: python manage.py test seofixer
Build guidance: use the button on the home page (no API key needed).
Add ANTHROPIC_API_KEY to .env and restart for AI suggestions.
GITHUB_TOKEN enables draft PRs; PAGESPEED_API_KEY enables Google scores.
Keep this demo on localhost. Public hosting needs user authentication,
request limits, and browser network isolation.
"""
import os
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / '.env')
SECRET_KEY = os.environ['DJANGO_SECRET_KEY']
DEBUG = True
ALLOWED_HOSTS = ['localhost', '127.0.0.1', '[::1]']

INSTALLED_APPS = [
    'django.contrib.contenttypes',
    'django.contrib.sessions',
]
MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]
ROOT_URLCONF = 'seofixer.urls'
TEMPLATES = [{
    'BACKEND': 'django.template.backends.django.DjangoTemplates',
    'DIRS': [BASE_DIR / 'seofixer' / 'templates'], 'APP_DIRS': True,
}]
DATABASES = {'default': {
    'ENGINE': 'django.db.backends.sqlite3',
    'NAME': BASE_DIR / 'db.sqlite3',
}}
WSGI_APPLICATION = 'seofixer.wsgi.application'
USE_TZ = True

ANTHROPIC_API_KEY = os.getenv('ANTHROPIC_API_KEY', '')
GITHUB_TOKEN = os.getenv('GITHUB_TOKEN', '')
PAGESPEED_API_KEY = os.getenv('PAGESPEED_API_KEY', '')
LLM_MODEL = os.getenv('LLM_MODEL', 'claude-opus-5-5')
LLM_MAX_TOKENS = 16000  # Opus 5.5 always thinks, and thinking counts toward this limit00
AGENT_MAX_TURNS = 3
MAX_FIXES = 10
MAX_BODY_CHARS = 50000
SEARCH_RESULTS = 3
CHROMA_DIR = BASE_DIR / 'chroma_db'
SCREENSHOT_DIR = BASE_DIR / 'screenshots'
SESSION_COOKIE_AGE = 86400

# Official Google sources; rebuild the local index to refresh their contents.
SEO_SOURCES = [
    'https://developers.google.com/search/docs/fundamentals/seo-starter-guide',
    'https://developers.google.com/search/docs/fundamentals/creating-helpful-content',
    'https://developers.google.com/search/docs/appearance/title-link',
    'https://developers.google.com/search/docs/appearance/snippet',
    'https://developers.google.com/search/docs/appearance/google-images',
    'https://developers.google.com/search/docs/appearance/structured-data/intro-structured-data',
    'https://developers.google.com/search/docs/appearance/page-experience',
    'https://developers.google.com/search/docs/crawling-indexing/links-crawlable',
    'https://developers.google.com/search/docs/crawling-indexing/consolidate-duplicate-urls',
    'https://developers.google.com/search/docs/crawling-indexing/robots-meta-tag',
    'https://developers.google.com/search/docs/crawling-indexing/mobile/mobile-sites-mobile-first-indexing',
    'https://developers.google.com/search/docs/essentials/spam-policies',
    # AI Overviews, AI Mode and AI assistants (checked 2026-10-30)
    'https://developers.google.com/search/docs/fundamentals/ai-optimization-guide',
    'https://developers.google.com/search/docs/appearance/ai-features',
    'https://developers.google.com/search/docs/fundamentals/using-gen-ai-content',
    'https://support.google.com/webmasters/answer/16908024',
    'https://developers.google.com/crawling/docs/crawlers-fetchers/google-common-crawlers',
    'https://developers.openai.com/api/docs/bots',
    'https://support.claude.com/en/articles/8896518-does-anthropic-crawl-data-from-the-web-and-how-can-site-owners-block-the-crawler',
    'https://docs.perplexity.ai/docs/resources/perplexity-crawlers',
    # Technical basics and local businesses
    'https://developers.google.com/search/docs/essentials/technical',
    'https://developers.google.com/search/docs/appearance/core-web-vitals',
    'https://developers.google.com/search/docs/crawling-indexing/robots/intro',
    'https://developers.google.com/search/docs/crawling-indexing/sitemaps/build-sitemap',
    'https://developers.google.com/search/docs/crawling-indexing/javascript/javascript-seo-basics',
    'https://developers.google.com/search/docs/appearance/structured-data/sd-policies',
    'https://developers.google.com/search/docs/appearance/structured-data/local-business',
    'https://support.google.com/business/answer/7091',
]
# Agents may web search these sites (subdomains included) when the local guidance falls short.
WEB_SEARCH_DOMAINS = ['developers.google.com', 'web.dev', 'schema.org', 'developer.mozilla.org', 'w3.org',
                      'support.google.com', 'bing.com', 'developers.openai.com', 'support.claude.com', 'docs.perplexity.ai']
