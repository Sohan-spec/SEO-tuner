#!/usr/bin/env node
// seofixer TUI: run `seofixer` inside your website folder and pick from a fixed menu.
// Each choice runs `python -m seofixer.cli <action> --project <folder> --url <preview>` and draws its JSON events.
import { spawn } from 'node:child_process';
import { existsSync, readdirSync, readFileSync } from 'node:fs';
import { readFile, stat } from 'node:fs/promises';
import http from 'node:http';
import os from 'node:os';
import path from 'node:path';
import readline from 'node:readline';
import { fileURLToPath } from 'node:url';
import React, { useEffect, useRef, useState } from 'react';
import { Box, Static, Text, render, useApp, useInput } from 'ink';

const h = React.createElement;
const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..'); // the seofixer repo
const PYTHON = path.join(ROOT, '.venv', 'bin', 'python');
const PROJECT = process.cwd();

// The fixed menu: plain English on the left, the flags it runs on the right.
const OPTIONS = [
  { label: 'Check my site', hint: 'Find SEO problems and get AI advice. Changes nothing.', action: 'check' },
  { label: 'Fix my site', hint: 'Apply the safe fixes to your files, show each one, then measure again.', action: 'fix' },
  { label: 'Speed up my images', hint: 'Shrink heavy images and add lazy loading. No AI needed.', action: 'images' },
  { label: 'Can Google and AI assistants find me?', hint: 'Check indexing, snippets, AI crawlers and your sitemap.', action: 'visibility' },
  { label: 'Suggest blog topics', hint: 'Up to 3 content ideas based on what your customers ask.', action: 'ideas' },
  { label: 'Measure again', hint: 'Compare your site now with the last check.', action: 'measure' },
  { label: 'Undo my last fixes', hint: 'Put your files back the way they were.', action: 'undo' },
  { label: "Refresh Google's guidance", hint: 'Download the latest official SEO and AI-search docs.', action: 'refresh' },
  { label: 'Quit', hint: '', action: null },
];

// ---------------- local preview server ----------------
const TYPES = { '.html': 'text/html', '.css': 'text/css', '.js': 'text/javascript', '.json': 'application/json', '.txt': 'text/plain',
  '.svg': 'image/svg+xml', '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.webp': 'image/webp', '.gif': 'image/gif' };

// Added only to previews opened with ?seofixer=<text>: scroll to each change and outline it, one after another.
const HIGHLIGHT = `<script>(() => {
  const marks = new URLSearchParams(location.search).getAll('seofixer');
  const style = document.createElement('style');
  style.textContent = '.seofixer-mark{outline:3px solid #e5c07b;outline-offset:6px;border-radius:3px}'
    + '#seofixer-card{position:fixed;left:16px;right:16px;top:16px;z-index:99999;padding:14px 18px;border-radius:8px;'
    + 'background:#111;color:#eee;font:14px/1.5 ui-monospace,Menlo,monospace;box-shadow:0 8px 30px #0006}';
  document.head.append(style);
  const find = (text) => {
    for (const img of document.images) if (img.getAttribute('src') === text) return img;
    const walk = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    while (walk.nextNode()) if (walk.currentNode.textContent.includes(text)) return walk.currentNode.parentElement;
  };
  let i = 0;
  const next = () => {
    document.querySelectorAll('.seofixer-mark').forEach((el) => el.classList.remove('seofixer-mark'));
    document.getElementById('seofixer-card')?.remove();
    const text = marks[i++ % marks.length], el = find(text);
    if (el) { el.classList.add('seofixer-mark'); el.scrollIntoView({ behavior: 'smooth', block: 'center' }); return; }
    const card = Object.assign(document.createElement('div'), { id: 'seofixer-card' });
    card.textContent = 'Changed in the page <head> (not visible on the page): ' + text;
    document.body.append(card); window.scrollTo({ top: 0, behavior: 'smooth' });
  };
  if (marks.length) { next(); setInterval(next, 2600); }
})();</script>`;

function startPreview(root) {
  const server = http.createServer(async (req, res) => {
    const url = new URL(req.url, 'http://localhost');
    let file = path.resolve(root, '.' + decodeURIComponent(url.pathname));
    if (file !== root && !file.startsWith(root + path.sep)) return res.writeHead(403).end(); // stay inside the folder
    try {
      if ((await stat(file)).isDirectory()) file = path.join(file, 'index.html');
      let body = await readFile(file);
      const type = TYPES[path.extname(file).toLowerCase()] || 'application/octet-stream';
      if (type === 'text/html' && url.searchParams.has('seofixer')) body = body.toString().replace(/<\/body>/i, HIGHLIGHT + '</body>');
      res.writeHead(200, { 'Content-Type': type, 'Cache-Control': 'no-store' }).end(body);
    } catch {
      res.writeHead(404).end('Not found');
    }
  });
  return new Promise((resolve) => server.listen(0, '127.0.0.1', () => resolve({ server, url: `http://127.0.0.1:${server.address().port}/` })));
}

// ---------------- the editor (Cursor or VS Code) ----------------
const IS_CURSOR = (process.env.__CFBundleIdentifier || '').includes('todesktop') || 'CURSOR_TRACE_ID' in process.env;
const IDE = IS_CURSOR
  ? { name: 'Cursor', scheme: 'cursor', bin: ['/Applications/Cursor.app/Contents/Resources/app/bin/cursor', 'cursor'], extensions: '.cursor' }
  : { name: 'VS Code', scheme: 'vscode', bin: ['/Applications/Visual Studio Code.app/Contents/Resources/app/bin/code', 'code'], extensions: '.vscode' };
const IN_IDE = process.env.TERM_PROGRAM === 'vscode';
const IDE_BIN = IDE.bin.find((b) => !b.startsWith('/') || existsSync(b));
const OPENER = process.platform === 'darwin' ? 'open' : 'xdg-open';

const run = (cmd, args) => spawn(cmd, args, { stdio: 'ignore', detached: true }).on('error', () => {}).unref();
const hasPreviewExtension = () => {
  try { return readdirSync(path.join(os.homedir(), IDE.extensions, 'extensions')).some((d) => d.startsWith('seofixer.seofixer-preview')); }
  catch { return false; }
};
const openInEditor = (file, line) => IN_IDE && run(IDE_BIN, ['-r', '-g', `${path.join(PROJECT, file)}:${line}`]);
function openPreview(url) {
  if (IN_IDE && hasPreviewExtension()) run(OPENER, [`${IDE.scheme}://seofixer.seofixer-preview/open?url=${encodeURIComponent(url)}`]);
  else run(OPENER, [url]); // no editor extension: the normal browser
}

// ---------------- drawing helpers ----------------
const SPIN = '⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏';
const BLOCKS = ' ▏▎▍▌▋▊▉';
const pad = (s, n) => String(s).padEnd(n);
const padL = (s, n) => String(s).padStart(n);
const bar = (f, n = 16) => { const x = Math.max(0, Math.min(1, f)) * n, full = Math.floor(x); return ('█'.repeat(full) + (full < n ? BLOCKS[Math.floor((x - full) * 8)] : '')).padEnd(n); };
const shortSource = (url) => (url || '').replace(/^https:\/\/(www\.)?/, '').replace('developers.google.com/search/docs/', '…/');
const SEVERITY = { high: 'red', medium: 'yellow', low: 'gray' };
const AGENTS = ['snippets', 'content', 'images', 'technical'];
const AGENT_NAME = { snippets: 'titles', content: 'content', images: 'images', technical: 'technical' };

let nextId = 0;
// A log line is a list of segments: plain strings or [text, {color, dimColor, bold}].
const line = (...segs) => ({ id: nextId++, segs });
const Line = ({ segs }) => h(Text, null, ...segs.map((s, i) => (typeof s === 'string' ? s : h(Text, { key: i, ...s[1] }, s[0]))));

function metricLines(rows) {
  const after = rows.some((r) => r.after !== null && r.after !== undefined);
  const out = [line(['  ' + pad('metric', 40) + padL('before', 10) + (after ? padL('after', 10) + padL('change', 12) : ''), { dimColor: true }])];
  for (const r of rows) {
    const show = (v) => (v === null || v === undefined ? 'n/a' : v);
    const color = r.verdict === 'better' ? 'green' : r.verdict === 'worse' ? 'red' : undefined;
    out.push(line('  ' + pad(r.name, 40), [padL(show(r.before), 10), { dimColor: !!after }],
      ...(after ? [[padL(show(r.after), 10), { color, bold: !!color }], [padL(r.change || '', 12), { color }]] : [])));
  }
  return out;
}

// Turn one backend event into log lines (and side effects for the editor).
function linesFor(e, ctx) {
  switch (e.event) {
    case 'step': return e.status === 'done' ? [line(['✓ ', { color: 'green' }], e.label)] : [];
    case 'issues': return [line(''), line(['Rule checks', { bold: true }], [`   ${e.items.length} issues`, { dimColor: true }]),
      ...e.items.map((i) => line('  ', ['✗ ', { color: 'red' }], [pad(i.severity, 8), { color: SEVERITY[i.severity] }], i.detail))];
    case 'agent':
      if (e.status === 'search') return [line(['    ' + pad(AGENT_NAME[e.agent], 11), { dimColor: true }], `searched “${e.query}”  `,
        [e.results.map(([s, src]) => `${s ?? '?'} ${src.split('/').pop()}`).join(' · '), { dimColor: true }])];
      if (e.status === 'web') return [line(['    ' + pad(AGENT_NAME[e.agent], 11), { dimColor: true }], ['web  ', { color: 'yellow' }], [e.urls.map(shortSource).join('  '), { dimColor: true }])];
      if (e.status === 'done') return [line('  ', ['✓ ', { color: 'green' }], pad(AGENT_NAME[e.agent], 11), `${e.fixes} fixes · score ${e.score}/100`)];
      if (e.status === 'failed') return [line('  ', ['✗ ', { color: 'red' }], pad(AGENT_NAME[e.agent], 11), 'could not finish')];
      return [];
    case 'advice': return [line(''), line(['Suggestions', { bold: true }], [`   ${e.fixes.length} · every one cited`, { dimColor: true }]),
      ...e.fixes.flatMap((f) => [
        line('  ', [pad(f.severity, 8), { color: SEVERITY[f.severity] }], f.issue, f.auto ? [' · auto-fixable', { color: 'green' }] : ''),
        line(['          ' + (shortSource(f.source) || 'no verified source'), { dimColor: true }])])];
    case 'usage': return [line([`  ${e.turns} turns · ${e.input_tokens.toLocaleString()} input · ${e.output_tokens.toLocaleString()} output tokens`, { dimColor: true }])];
    case 'change':
      ctx.changes.push(e);
      return [line('  ', ['+ ', { color: 'green' }], pad(e.label.length > 42 ? e.label.slice(0, 41) + '…' : e.label, 44),
        [e.line ? `${e.file}:${e.line}` : e.file, { dimColor: true }])];
    case 'skipped': return [line(['  – ' + e.label + ': ' + e.reason, { dimColor: true }])];
    case 'metrics': return [line(''), line([ctx.action === 'check' ? 'Measurements' : 'Before and after', { bold: true }]), ...metricLines(e.rows)];
    case 'facts': return [line(''), line(['Can Google and AI assistants find you?', { bold: true }]),
      ...e.items.map((f) => line('  ', f.ok === true ? ['✓ ', { color: 'green' }] : f.ok === false ? ['✗ ', { color: 'red' }] : ['• ', { dimColor: true }], f.text))];
    case 'ideas': return [line(''), line(['Blog topic ideas', { bold: true }]), ...e.text.split(/\n+/).map((t) => line('  ' + t)),
      line(['  ' + shortSource(e.source), { dimColor: true }])];
    case 'restored': return [line(e.files.length ? ['✓ ', { color: 'green' }] : '', e.files.length ? `Restored ${e.files.length} files: ${e.files.join(', ')}` : 'Nothing to undo.')];
    case 'refreshed': return [line(['✓ ', { color: 'green' }], `Downloaded ${e.stored} of ${e.total} sources`, e.failures.length ? [`  ${e.failures.length} failed`, { color: 'yellow' }] : '')];
    case 'warning': return [line(['! ' + e.message, { color: 'yellow' }])];
    case 'error': return [line(['✗ ' + e.message, { color: 'red' }])];
    case 'done': return [line(''), line([`Done in ${e.seconds}s`, { dimColor: true }])];
    default: return [];
  }
}

// ---------------- the app ----------------
function App({ preview }) {
  const { exit } = useApp();
  const [log, setLog] = useState([line(['seofixer', { bold: true }], [`  ·  ${path.basename(PROJECT)}  ·  preview ${preview.url}`, { dimColor: true }])]);
  const [mode, setMode] = useState('menu'); // menu | running | finished
  const [selected, setSelected] = useState(0);
  const [live, setLive] = useState({ step: null, agents: {}, search: null, note: null });
  const [tick, setTick] = useState(0);
  const child = useRef(null);
  const add = (lines) => lines.length && setLog((prev) => [...prev, ...lines]);

  useEffect(() => {
    if (mode !== 'running' && !live.note) return;
    const timer = setInterval(() => setTick((t) => t + 1), 80);
    return () => clearInterval(timer);
  }, [mode, live.note]);

  function start(option) {
    const args = ['-m', 'seofixer.cli', option.action, '--project', PROJECT, '--url', preview.url];
    const ctx = { action: option.action, changes: [] };
    add([line(''), line(['$ seofixer ' + option.action + ' --project . --url ' + preview.url, { dimColor: true }])]);
    setLive({ step: null, agents: {}, search: null, note: null });
    setMode('running');
    const proc = spawn(PYTHON, args, { cwd: ROOT, env: { ...process.env, PYTHONUNBUFFERED: '1' } });
    child.current = proc;
    const errors = [];
    let failed = false;
    readline.createInterface({ input: proc.stderr }).on('line', (l) => errors.push(l) > 20 && errors.shift());
    readline.createInterface({ input: proc.stdout }).on('line', (raw) => {
      let e;
      try { e = JSON.parse(raw); } catch { return; }
      failed ||= e.event === 'error';
      if (e.event === 'step') setLive((l) => ({ ...l, step: e.status === 'running' ? e.label : null }));
      if (e.event === 'agent') setLive((l) => ({ ...l, agents: { ...l.agents, [e.agent]: e }, search: e.status === 'search' ? e : l.search }));
      add(linesFor(e, ctx));
    });
    proc.on('close', (code) => {
      child.current = null;
      if (code && !failed) add([line(['✗ The backend stopped unexpectedly:', { color: 'red' }]), ...errors.slice(-5).map((l) => line(['  ' + l, { dimColor: true }]))]);
      showChanges(ctx.changes);
      setLive((l) => ({ ...l, step: null, agents: {}, search: null }));
      setMode('finished');
    });
  }

  // Walk through the edits in the editor, one every 1.5s, and open the page scrolled to them.
  function showChanges(changes) {
    const marks = changes.map((c) => c.mark).filter(Boolean);
    if (!marks.length) return;
    openPreview(preview.url + '?' + marks.map((m) => 'seofixer=' + encodeURIComponent(m)).join('&'));
    const edits = changes.filter((c) => c.line);
    edits.forEach((c, i) => setTimeout(() => openInEditor(c.file, c.line), 600 + i * 1500));
    const where = IN_IDE ? `${IDE.name} and the preview` : 'your browser';
    setLive((l) => ({ ...l, note: `Showing ${edits.length} changes in ${where}…` }));
    setTimeout(() => setLive((l) => ({ ...l, note: null })), 900 + edits.length * 1500);
  }

  useInput((input, key) => {
    if (mode === 'running') return; // no typing while a job runs (Ctrl+C still quits)
    if (mode === 'finished') { if (key.return || input === ' ') setMode('menu'); else if (input === 'q') exit(); return; }
    if (key.upArrow) setSelected((s) => (s + OPTIONS.length - 1) % OPTIONS.length);
    else if (key.downArrow) setSelected((s) => (s + 1) % OPTIONS.length);
    else if (input === 'q') exit();
    else if (key.return) OPTIONS[selected].action ? start(OPTIONS[selected]) : exit();
  });

  useEffect(() => () => child.current?.kill(), []);

  const spin = SPIN[tick % SPIN.length];
  const agentLines = AGENTS.filter((a) => live.agents[a]).map((a) => {
    const e = live.agents[a];
    const text = { thinking: 'thinking…', search: `search “${e.query}”`, web: 'web_search ' + (e.urls || []).map(shortSource)[0], done: `${e.fixes} fixes · score ${e.score}/100`, failed: 'could not finish' }[e.status];
    const mark = e.status === 'done' ? h(Text, { color: 'green' }, '✓') : e.status === 'failed' ? h(Text, { color: 'red' }, '✗') : h(Text, { dimColor: true }, spin);
    return h(Text, { key: a }, '  ', mark, ' ', pad(AGENT_NAME[a], 11), h(Text, { dimColor: e.status !== 'done' }, text));
  });

  return h(Box, { flexDirection: 'column' },
    h(Static, { items: log }, (item) => h(Line, { key: item.id, segs: item.segs })),
    mode === 'running' && h(Box, { flexDirection: 'column', marginTop: 1 },
      live.step && h(Text, null, h(Text, { dimColor: true }, spin + ' '), live.step),
      agentLines.length > 0 && h(Box, { flexDirection: 'column', marginTop: 1 }, h(Text, { bold: true }, 'Agents', h(Text, { dimColor: true }, '   4 running in parallel')), ...agentLines),
      live.search && h(Box, { flexDirection: 'column', marginTop: 1 },
        h(Text, null, h(Text, { dimColor: true }, '  chroma  '), `“${live.search.query}”`, h(Text, { dimColor: true }, '  cosine similarity')),
        ...live.search.results.map(([score, src], i) => h(Text, { key: i }, '    ', (score ?? 0).toFixed(2), '  ', h(Text, { color: 'gray' }, bar(score ?? 0)), '  ', shortSource(src))))),
    mode === 'finished' && h(Box, { marginTop: 1 },
      live.note ? h(Text, null, h(Text, { dimColor: true }, spin + ' '), live.note) : h(Text, { dimColor: true }, 'Press Enter to go back to the menu · q to quit')),
    mode === 'menu' && h(Box, { flexDirection: 'column', marginTop: 1 },
      h(Text, null, h(Text, { bold: true }, 'What would you like to do?'), h(Text, { dimColor: true }, '   ↑ ↓ to move · Enter to choose · q to quit')),
      ...OPTIONS.map((o, i) => h(Text, { key: o.label },
        i === selected ? h(Text, { color: 'green' }, '› ') : '  ', h(Text, { bold: i === selected }, o.label))),
      h(Text, { dimColor: true }, '\n  ' + (OPTIONS[selected].hint || 'Close seofixer.'))));
}

// ---------------- start ----------------
if (!existsSync(path.join(PROJECT, 'index.html'))) {
  console.log('seofixer works on a website folder. Open the folder that contains your index.html and run `seofixer` again.');
  process.exit(1);
}
if (/\{\{|\{%/.test(readFileSync(path.join(PROJECT, 'index.html'), 'utf8'))) { // the preview can't render it, and edits would hit template code
  console.log('This index.html is a template (Django, Flask or Jinja), not a finished page. Run seofixer in a folder with plain HTML.');
  process.exit(1);
}
if (!existsSync(PYTHON)) {
  console.log(`seofixer's Python setup is missing (${PYTHON}). See the setup notes at the top of seofixer/settings.py.`);
  process.exit(1);
}
const preview = await startPreview(PROJECT);
const app = render(h(App, { preview }));
await app.waitUntilExit();
preview.server.close();
process.exit(0);
