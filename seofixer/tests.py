"""Run with: python manage.py test seofixer

Real HTML parsing, Chroma vectors, image processing, and Django requests.
Claude, GitHub, and PageSpeed responses are simulated; no keys are needed.
"""
import base64
import io
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.conf import settings
from django.test import Client, SimpleTestCase, TestCase, override_settings
from PIL import Image
from . import ai, audit, measurements, prompts
from . import github as repo

HTML = '''<html lang="en"><head><title>Bikes for students</title>
<meta name="description" content="Buy and repair student bicycles.">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="canonical" href="https://example.com/"></head><body>
<h1>Student bicycles</h1><h2>Repairs</h2><p>We repair bicycles on campus.</p>
<img src="/decor.png" alt=""><img src="/bike.jpg">
<a href="/repairs">Bicycle repairs</a></body></html>'''
SOURCE = settings.SEO_SOURCES[0]


def png(size=(100, 100), color='white'):
    buffer = io.BytesIO()
    Image.new('RGB', size, color).save(buffer, 'PNG')
    return buffer.getvalue()


def fix(find='Bikes for students', replace='Student bicycle repairs'):
    return {'issue': 'Improve the title', 'severity': 'medium', 'why': 'Describe the service.',
            'find': find, 'replace': replace, 'source': SOURCE}


def model_reply(name, data):
    return SimpleNamespace(content=[SimpleNamespace(type='tool_use', name=name, input=data, id='tool_1')],
                           usage=SimpleNamespace(input_tokens=20, output_tokens=10))


class ParsingTests(SimpleTestCase):
    def test_extracts_page_and_decorative_alt_is_valid(self):
        data = audit.extract_page_data(HTML, 'https://example.com/')
        self.assertEqual(data['title'], 'Bikes for students')
        self.assertEqual(data['images_missing_alt_count'], 1)
        self.assertEqual(data['images_missing_alt'], ['/bike.jpg'])
        self.assertEqual(data['internal_links'], 1)
        self.assertEqual(data['word_count'], 10)

    def test_rules_do_not_invent_word_count_or_jsonld_requirements(self):
        data = audit.extract_page_data(HTML, 'https://example.com/')
        issues = audit.run_checks(data)
        self.assertEqual([i['area'] for i in issues], ['images'])

    def test_headings_after_the_first_forty_are_checked(self):
        data = audit.extract_page_data('<html><body>' + '<h2>Section</h2>' * 50 + '<h1>Main</h1><h6>End</h6></body></html>', 'https://example.com/')
        issues = audit.run_checks(data)
        self.assertFalse(any('No H1' in i['detail'] for i in issues))
        self.assertTrue(any('levels skip' in i['detail'] for i in issues))

    def test_noindex_is_a_directive_not_a_substring(self):
        for robots, flagged in [('NOINDEX, follow', True), ('not-noindex', False)]:
            data = audit.extract_page_data(HTML.replace('<head>', f'<head><meta name="robots" content="{robots}">'), 'https://example.com/')
            self.assertEqual(any('noindex' in i['detail'] for i in audit.run_checks(data)), flagged)

    def test_chunking_keeps_overlap_and_end(self):
        words = [f'word{i}' for i in range(250)]
        chunks = ai.chunk_text(' '.join(words))
        self.assertEqual(chunks[0].split()[-30:], chunks[1].split()[:30])
        self.assertEqual(chunks[-1].split()[-1], 'word249')
        self.assertEqual(ai.chunk_text(''), [])
        with self.assertRaises(ValueError):
            ai.chunk_text('text', size=10, overlap=10)

    @patch('seofixer.audit.socket.getaddrinfo')
    def test_url_checks_private_and_mixed_dns(self, dns):
        public = (2, 1, 6, '', ('93.184.215.14', 443))
        private = (2, 1, 6, '', ('127.0.0.1', 443))
        dns.return_value = [public]
        self.assertEqual(audit.validate_url('https://example.com'), 'https://example.com')
        for url in ['file:///etc/passwd', 'https://user:password@example.com', 'https://example.com:8000']:
            with self.subTest(url=url), self.assertRaises(ValueError):
                audit.validate_url(url)
        dns.return_value = [public, private]
        with self.assertRaises(ValueError):
            audit.validate_url('https://example.com')

    def test_repo_names(self):
        for text in ['owner/repo', 'https://github.com/owner/repo.git/']:
            self.assertEqual(repo.parse_repo(text), 'owner/repo')
        for text in ['https://evil.test/github.com/owner/repo', 'owner/repo/extra', 'owner/..', 'owner/repo?x=1']:
            with self.subTest(text=text), self.assertRaises(ValueError):
                repo.parse_repo(text)


class AgentTests(SimpleTestCase):
    def setUp(self):
        self.page = audit.extract_page_data(HTML, 'https://example.com/')

    @override_settings(ANTHROPIC_API_KEY='')
    @patch('seofixer.ai.get_collection')
    def test_missing_key_keeps_rule_only_mode(self, collection):
        report, usage = ai.run_all_agents(self.page, [])
        self.assertIsNone(report['score'])
        self.assertIn('ANTHROPIC_API_KEY', report['failures'][0])
        collection.assert_not_called()

    @override_settings(ANTHROPIC_API_KEY='test')
    @patch('seofixer.ai.get_collection')
    def test_empty_knowledge_base_has_actionable_message(self, collection):
        collection.return_value.count.return_value = 0
        report, usage = ai.run_all_agents(self.page, [])
        self.assertIn('Build the SEO knowledge base', report['failures'][0])

    @override_settings(ANTHROPIC_API_KEY='test')
    @patch('seofixer.ai.search_guidelines', return_value=[{'source': SOURCE, 'text': 'Write descriptive titles.'}])
    @patch('anthropic.Anthropic')
    def test_agent_searches_then_validates_report(self, anthropic, search):
        anthropic.return_value.messages.create.side_effect = [
            model_reply('search_seo_guidelines', {'query': 'good page titles'}),
            model_reply('submit_report', {'score': 80, 'summary': 'Improve the title.', 'fixes': [fix()]}),
        ]
        report, usage = ai.run_agent(prompts.SPECIALISTS[0], self.page, [])
        self.assertEqual(report['fixes'][0]['source'], SOURCE)
        self.assertEqual(usage, {'input_tokens': 40, 'output_tokens': 20, 'turns': 2})
        self.assertEqual(anthropic.return_value.messages.create.call_args_list[0].kwargs['tool_choice']['name'], 'search_seo_guidelines')

    def test_fabricated_citations_and_find_text_cannot_be_auto_applied(self):
        proposed = fix()
        proposed['source'] = 'javascript:alert(1)'
        result = ai.validate_report({'score': 70, 'summary': 'Review.', 'fixes': [proposed]}, {SOURCE}, self.page)
        self.assertEqual(result['fixes'][0]['source'], '')
        self.assertEqual(result['fixes'][0]['find'], '')
        result = ai.validate_report({'score': 70, 'summary': 'Review.', 'fixes': [fix(find='Imaginary text')]}, {SOURCE}, self.page)
        self.assertEqual(result['fixes'][0]['find'], '')
        with self.assertRaises(ValueError):
            ai.validate_report({'score': 500, 'summary': 'Bad.', 'fixes': []}, {SOURCE}, self.page)

    @override_settings(ANTHROPIC_API_KEY='test')
    @patch('seofixer.ai.get_collection')
    def test_all_four_specialists_really_run_in_parallel(self, collection):
        collection.return_value.count.return_value = 3
        barrier = threading.Barrier(4)
        def run(specialist, page, issues):
            barrier.wait(timeout=5)
            return {'score': 80, 'summary': specialist['key'], 'fixes': [fix()]}, {'input_tokens': 1, 'output_tokens': 2, 'turns': 2}
        with patch('seofixer.ai.run_agent', side_effect=run):
            report, usage = ai.run_all_agents(self.page, [])
        self.assertEqual(len(report['areas']), 4)
        self.assertEqual(len(report['fixes']), 1)
        self.assertEqual(usage['turns'], 8)

    @override_settings(ANTHROPIC_API_KEY='test')
    @patch('seofixer.ai.get_collection')
    @patch('seofixer.ai.run_agent', side_effect=RuntimeError('fake API outage'))
    def test_all_agent_failures_return_a_report_instead_of_crashing(self, agent, collection):
        collection.return_value.count.return_value = 3
        with self.assertLogs('seofixer.ai', level='ERROR'):
            report, usage = ai.run_all_agents(self.page, [])
        self.assertEqual(len(report['failures']), 4)
        self.assertIsNone(report['score'])


class ImageAndEditTests(SimpleTestCase):
    def test_reused_image_uses_its_largest_display_width(self):
        image = {'url': 'https://example.com/photo.png', 'src': '/photo.png',
                 'shown_width': 100, 'natural_width': 1000, 'top': 900,
                 'bytes': 200000, 'loading': '', 'fetchpriority': ''}
        items = measurements.find_performance_issues([image, {**image, 'shown_width': 500}])
        self.assertTrue(all(item['needed_width'] == 1000 for item in items))
        self.assertTrue(all(not item['oversized'] for item in items))

    def test_text_edit_requires_one_occurrence_across_files(self):
        files = {'one.html': {'text': 'old old', 'changed': False}, 'two.html': {'text': 'old', 'changed': False}}
        self.assertIsNone(repo.replace_once_in_repo(files, 'old', 'new'))
        files = {'one.html': {'text': '<title>old</title>', 'changed': False}}
        self.assertEqual(repo.replace_once_in_repo(files, 'old', 'new'), 'one.html')
        self.assertEqual(files['one.html']['text'], '<title>new</title>')
        self.assertIsNone(repo.replace_once_in_repo(files, '', 'new'))

    def test_image_attributes_are_not_duplicated(self):
        files = {'one.html': {'text': '<img src="/photo.jpg" loading="eager">', 'changed': False}}
        self.assertEqual(repo.add_image_attribute(files, '/photo.jpg', 'loading', 'lazy'), 'one.html')
        self.assertEqual(files['one.html']['text'].count('loading='), 1)
        self.assertIn('loading="lazy"', files['one.html']['text'])
        files['one.html']['text'] = '<img src="/photo.jpg"/>'
        self.assertEqual(repo.add_image_attribute(files, '/photo.jpg', 'loading', 'lazy'), 'one.html')
        self.assertIn('loading="lazy"', files['one.html']['text'])

    def test_ambiguous_image_paths_are_not_guessed(self):
        self.assertEqual(repo.repo_path_for('https://example.com/img/a.png', ['public/img/a.png']), 'public/img/a.png')
        self.assertIsNone(repo.repo_path_for('https://example.com/img/a.png', ['public/img/a.png', 'static/img/a.png']))

    def test_image_resize_preserves_format_and_saves_bytes(self):
        original = png((2000, 1000), 'blue')
        smaller = repo.optimize_image(original, 400)
        self.assertLess(len(smaller), len(original) * .9)
        image = Image.open(io.BytesIO(smaller))
        self.assertEqual(image.format, 'PNG')
        self.assertEqual(image.size, (400, 200))
        with self.assertRaises(ValueError):
            repo.optimize_image(original, -1)

    def test_screenshot_difference_includes_new_page_area(self):
        with tempfile.TemporaryDirectory() as folder, override_settings(SCREENSHOT_DIR=Path(folder)):
            shot = measurements.save_screenshot(png((100, 100)))
            same = measurements.visual_difference(shot, png((100, 100)))
            self.assertEqual(same['percent'], 0)
            after = Image.new('RGB', (100, 200), 'black')
            after.paste(Image.new('RGB', (100, 100), 'white'), (0, 0))
            buffer = io.BytesIO()
            after.save(buffer, 'PNG')
            changed = measurements.visual_difference(shot, buffer.getvalue())
            self.assertEqual(changed['percent'], 50)
            self.assertEqual(changed['height_change'], 100)
            self.assertIsNone(measurements.visual_difference('../secret', buffer.getvalue()))

    def test_comparison_respects_metric_direction(self):
        rows = measurements.compare({'Oversized images': 3, 'Google SEO score': 80}, {'Oversized images': 1, 'Google SEO score': 85})
        changed = {row['name']: row for row in rows}
        self.assertEqual(changed['Oversized images']['verdict'], 'better')
        self.assertEqual(changed['Google SEO score']['change'], '+5')


class RetrievalTests(SimpleTestCase):
    def test_real_cosine_retrieval_and_refresh_removes_stale_chunks(self):
        with tempfile.TemporaryDirectory() as folder, override_settings(CHROMA_DIR=Path(folder), SEO_SOURCES=[SOURCE]):
            ai.get_collection.cache_clear()
            store = ai.get_collection()
            self.assertEqual(ai.search_guidelines('titles'), [])
            store.upsert(ids=[SOURCE+'#0', SOURCE+'#99'],
                         documents=['Use descriptive page titles that explain the page topic.', 'Old stale guidance.'],
                         metadatas=[{'source': SOURCE}, {'source': SOURCE}])
            self.assertEqual(ai.search_guidelines('How should a page title be written?')[0]['source'], SOURCE)
            response = Mock(text='<article>Write accurate page titles that describe the content.</article>')
            with patch('seofixer.ai.requests.get', return_value=response):
                self.assertEqual(ai.ingest()['stored'], 1)
            self.assertEqual(store.count(), 1)
            self.assertEqual(store.configuration['hnsw']['space'], 'cosine')
            ai.get_collection.cache_clear()


class GitHubTests(SimpleTestCase):
    @patch('seofixer.github.github')
    def test_image_and_loading_edits_reach_the_same_commit(self, github):
        original = png((2000, 1000), 'blue')
        uploaded = []
        def respond(method, endpoint, **kwargs):
            if endpoint == '/repos/owner/repo': return {'default_branch': 'main'}
            if '/git/ref/heads/' in endpoint: return {'object': {'sha': 'base'}}
            if method == 'GET' and '/git/trees/' in endpoint:
                return {'sha': 'tree', 'tree': [
                    {'path': 'index.html', 'type': 'blob', 'mode': '100644', 'size': 50, 'sha': 'html'},
                    {'path': 'photo.png', 'type': 'blob', 'mode': '100644', 'size': len(original), 'sha': 'image'},
                ]}
            if method == 'GET' and '/git/blobs/' in endpoint:
                data = original if endpoint.endswith('/image') else b'<img src="/photo.png" loading="eager">'
                return {'content': base64.b64encode(data).decode()}
            if method == 'POST' and endpoint.endswith('/git/blobs'):
                uploaded.append(base64.b64decode(kwargs['json']['content']))
            if endpoint.endswith('/pulls'): return {'html_url': 'https://github.com/owner/repo/pull/1'}
            return {'sha': 'new'}
        github.side_effect = respond
        item = {'url': 'https://example.com/photo.png', 'src': '/photo.png',
                'lazy': True, 'priority': False, 'oversized': True, 'heavy': False, 'needed_width': 400}
        result = repo.create_pull_request('owner/repo', [], 'https://example.com/', [item])
        self.assertEqual(len(result['images']), 1)
        self.assertEqual(len(result['attributes']), 1)
        self.assertEqual(len(uploaded), 2)
        self.assertTrue(any(b'loading="lazy"' in content for content in uploaded))

    @patch('seofixer.github.github')
    def test_builds_one_commit_and_a_draft_pr(self, github):
        calls = []
        def respond(method, endpoint, **kwargs):
            calls.append((method, endpoint, kwargs))
            if endpoint == '/repos/owner/repo': return {'default_branch': 'main'}
            if '/git/ref/heads/' in endpoint: return {'object': {'sha': 'base'}}
            if method == 'GET' and '/git/trees/' in endpoint:
                return {'sha': 'tree', 'truncated': False, 'tree': [
                    {'path': 'index.html', 'type': 'blob', 'mode': '100644', 'size': 50, 'sha': 'html'}]}
            if method == 'GET' and '/git/blobs/' in endpoint:
                return {'content': base64.b64encode(b'<title>Bikes for students</title>').decode()}
            if endpoint.endswith('/pulls'): return {'html_url': 'https://github.com/owner/repo/pull/1'}
            return {'sha': 'new'}
        github.side_effect = respond
        result = repo.create_pull_request('owner/repo', [fix()], 'https://example.com', [])
        self.assertEqual(len(result['applied']), 1)
        self.assertEqual(sum('/git/commits' in p for m,p,k in calls), 1)
        self.assertTrue(calls[-1][2]['json']['draft'])
        self.assertEqual(calls[-1][2]['json']['base'], 'main')
        self.assertFalse(any(m == 'PATCH' for m,p,k in calls))

    @patch('seofixer.github.github')
    def test_truncated_repo_tree_cannot_be_edited(self, github):
        github.side_effect = [{'default_branch': 'main'}, {'object': {'sha': 'base'}}, {'truncated': True}]
        with self.assertRaisesMessage(ValueError, 'incomplete'):
            repo.create_pull_request('owner/repo', [fix()], 'https://example.com', [])
        self.assertTrue(all(call.args[0] == 'GET' for call in github.call_args_list))


class PageFlowTests(TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.config = override_settings(SCREENSHOT_DIR=Path(self.folder.name), ANTHROPIC_API_KEY='', GITHUB_TOKEN='')
        self.config.enable()
        self.addCleanup(self.config.disable)
        self.addCleanup(self.folder.cleanup)

    def audit(self):
        data = audit.extract_page_data(HTML, 'https://example.com/')
        measured = (data, audit.run_checks(data), {'Rule checks passed': 10}, [], png())
        with patch('seofixer.audit.validate_url', side_effect=lambda value: value), patch('seofixer.measurements.measure', return_value=measured):
            return self.client.post('/', {'action': 'audit', 'url': 'https://example.com/', 'repo': 'owner/repo'}, follow=True)

    def test_page_loads_and_csrf_is_enforced(self):
        self.assertContains(self.client.get('/'), 'SEO Fixer')
        self.assertEqual(Client(enforce_csrf_checks=True).post('/', {'action': 'audit'}).status_code, 403)

    def test_keyless_audit_preserves_measurements_and_rules(self):
        response = self.audit()
        self.assertContains(response, '1 images lack an alt attribute')
        self.assertContains(response, 'Rule checks passed')
        self.assertContains(response, 'ANTHROPIC_API_KEY')
        self.assertNotContains(response, 'name="fixes"')
        self.assertIn('before', self.client.session['audit'])

    @override_settings(GITHUB_TOKEN='test')
    @patch('seofixer.github.create_pull_request', return_value={'pr_url': 'https://github.com/owner/repo/pull/1', 'applied': [], 'skipped': [], 'images': [], 'attributes': []})
    def test_pr_uses_server_data_ignores_tampering_and_does_not_repeat(self, create):
        self.audit()
        session = self.client.session
        state = session['audit']
        state['report']['fixes'] = [fix()]
        session['audit'] = state
        session.save()
        payload = {'action': 'pr', 'audit_id': state['audit_id'], 'selected': ['0'],
                   'repo': 'attacker/repo', 'fixes': '[{"find":"safe","replace":"malicious"}]'}
        self.client.post('/', payload, follow=True)
        self.client.post('/', payload, follow=True)
        create.assert_called_once_with('owner/repo', [fix()], 'https://example.com/', [])

    def test_old_form_and_negative_selection_are_rejected(self):
        self.audit()
        response = self.client.post('/', {'action': 'measure', 'audit_id': 'old'})
        self.assertContains(response, 'outdated')
        with override_settings(GITHUB_TOKEN='test'):
            response = self.client.post('/', {'action': 'pr', 'audit_id': self.client.session['audit']['audit_id'], 'selected': ['-1']})
        self.assertContains(response, 'Invalid fix selection')

    def test_measure_again_keeps_original_baseline(self):
        self.audit()
        state = self.client.session['audit']
        with patch('seofixer.audit.validate_url', side_effect=lambda value: value), patch('seofixer.measurements.measure', return_value=({}, [], {'Rule checks passed': 11}, [], png())):
            response = self.client.post('/', {'action': 'measure', 'audit_id': state['audit_id']}, follow=True)
        self.assertContains(response, 'before and after')
        self.assertEqual(self.client.session['audit']['before'], {'Rule checks passed': 10})
        self.assertEqual(self.client.session['audit']['after'], {'Rule checks passed': 11})
