"""The single-page HTML/CSS/JS served by the read-only web dashboard.

Kept as a module constant so :mod:`projects_orchestrator.web` stays focused on
routing. The page polls ``/api/status`` and renders status cards; it ships no
external assets and sends no state-changing requests (ADR-004).

Security: project metadata (name, description, branch, container names) is
untrusted — it comes from marker files and git/docker output — so the DOM is
built with ``createElement`` + ``textContent``, never string-interpolated
markup.
"""

from __future__ import annotations

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>projects-orchestrator · dashboard</title>
<style>
  :root { color-scheme: dark light;
    --bg:#0b0f14; --panel:#131a22; --edge:#232f3a; --fg:#d7e0e8; --muted:#7d8b98; --faint:#4b5866;
    --accent:#3fd0c9; --ok:#54c98a; --warn:#e6b24d; --idle:#8592a0;
    --mono:ui-monospace,"SF Mono",Menlo,monospace; --sans:ui-sans-serif,system-ui,sans-serif; }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--fg); font-family:var(--sans); }
  header { position:sticky; top:0; background:linear-gradient(var(--bg),rgba(11,15,20,.85));
    padding:1.1rem 1.5rem; border-bottom:1px solid var(--edge); display:flex; align-items:baseline; gap:.7rem; }
  header .p { font-family:var(--mono); color:var(--accent); }
  header h1 { margin:0; font-size:1.15rem; font-weight:650; }
  header .meta { margin-left:auto; font-family:var(--mono); font-size:.8rem; color:var(--muted); }
  main { padding:1.5rem; display:grid; gap:1rem; grid-template-columns:repeat(auto-fill,minmax(310px,1fr)); }
  .card { background:var(--panel); border:1px solid var(--edge); border-radius:12px; padding:1rem 1.1rem; }
  .card.up { border-color:color-mix(in srgb,var(--ok) 45%,var(--edge)); }
  .top { display:flex; align-items:center; gap:.5rem; }
  .dot { width:9px; height:9px; border-radius:50%; background:var(--faint); flex:none; }
  .up .dot { background:var(--ok); box-shadow:0 0 0 3px color-mix(in srgb,var(--ok) 25%,transparent); }
  h2 { margin:0; font-size:1.03rem; font-weight:620; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .pill { margin-left:auto; font-family:var(--mono); font-size:.66rem; text-transform:uppercase;
    padding:.15rem .5rem; border-radius:99px; letter-spacing:.04em; }
  .clean { color:var(--ok); background:color-mix(in srgb,var(--ok) 18%,transparent); }
  .dirty { color:var(--warn); background:color-mix(in srgb,var(--warn) 18%,transparent); }
  .unversioned { color:var(--idle); background:color-mix(in srgb,var(--idle) 16%,transparent); }
  .desc { color:var(--muted); font-size:.88rem; margin:.5rem 0 .7rem; min-height:1.3em; }
  .facts { display:flex; flex-wrap:wrap; gap:.35rem; font-family:var(--mono); font-size:.72rem; }
  .tag { border:1px solid var(--edge); border-radius:6px; padding:.1rem .45rem; color:var(--muted); }
  .tag.on { color:var(--ok); border-color:color-mix(in srgb,var(--ok) 40%,var(--edge)); }
</style>
</head>
<body>
  <header>
    <span class="p">&#9656;</span><h1>projects-orchestrator</h1>
    <span class="meta" id="meta">connecting…</span>
  </header>
  <main id="grid"></main>
<script>
const grid = document.getElementById('grid');
const meta = document.getElementById('meta');
const VCS = {clean:1, dirty:1, unversioned:1};

function tag(text, on) {
  const s = document.createElement('span');
  s.className = on ? 'tag on' : 'tag';
  s.textContent = text;
  return s;
}

function card(p) {
  const el = document.createElement('div');
  el.className = p.running ? 'card up' : 'card';

  const top = document.createElement('div'); top.className = 'top';
  const dot = document.createElement('span'); dot.className = 'dot';
  const h2 = document.createElement('h2'); h2.textContent = p.name;
  const pill = document.createElement('span');
  pill.className = VCS[p.vcs_state] ? 'pill ' + p.vcs_state : 'pill';
  pill.textContent = p.vcs_state;
  top.append(dot, h2, pill);

  const desc = document.createElement('div'); desc.className = 'desc';
  desc.textContent = p.description || '\\u2014';

  const facts = document.createElement('div'); facts.className = 'facts';
  facts.append(tag('branch ' + p.branch));
  if (p.ports.length) facts.append(tag(':' + p.ports.join(' :'), true));
  if (p.containers.length) facts.append(tag(p.containers.length + ' container(s)', true));
  if (p.run) facts.append(tag('run ' + p.run));
  if (p.run_source !== 'none') facts.append(tag(p.run_source));

  el.append(top, desc, facts);
  return el;
}

async function refresh() {
  try {
    const r = await fetch('/api/status');
    const data = await r.json();
    grid.replaceChildren(...data.map(card));
    const up = data.filter(p => p.running).length;
    meta.textContent = data.length + ' project(s) · ' + up + ' running';
  } catch (e) { meta.textContent = 'disconnected'; }
}
refresh();
setInterval(refresh, 5000);
</script>
</body>
</html>
"""
