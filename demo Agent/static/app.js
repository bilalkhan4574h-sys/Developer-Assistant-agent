async function loadTools() {
  const res = await fetch('/api/tools');
  const data = await res.json();
  const list = document.getElementById('tool-list');
  const sel = document.getElementById('invoke-tool');
  list.innerHTML = '';
  sel.innerHTML = '';
  (data.tools || []).forEach(t => {
    const d = document.createElement('div');
    d.className = 'tool';
    d.innerHTML = `<strong>${t.name}</strong>: ${t.description} <em>[${t.type}]</em>`;
    list.appendChild(d);
    const opt = document.createElement('option');
    opt.value = t.name;
    opt.text = t.name;
    opt.dataset.spec = JSON.stringify(t);
    sel.appendChild(opt);
  });
  renderParams();
}

function renderParams() {
  const sel = document.getElementById('invoke-tool');
  const opt = sel.selectedOptions[0];
  const area = document.getElementById('params-area');
  area.innerHTML = '';
  if (!opt) return;
  const spec = JSON.parse(opt.dataset.spec);
  const params = spec.params || {};
  Object.keys(params).forEach(k => {
    const div = document.createElement('div');
    div.innerHTML = `<label>${k}: <input data-key="${k}" value="${params[k]}"></label>`;
    area.appendChild(div);
  });
  // add an ad-hoc params textbox
  const extra = document.createElement('div');
  extra.innerHTML = `<label>Extra (JSON): <input id="extra-params" placeholder='{"q":"python"}'></label>`;
  area.appendChild(extra);
}

async function invokeTool() {
  const sel = document.getElementById('invoke-tool');
  const opt = sel.selectedOptions[0];
  if (!opt) return;
  const name = opt.value;
  const inputs = document.querySelectorAll('#params-area input[data-key]');
  const params = {};
  inputs.forEach(i => {
    const k = i.dataset.key;
    params[k] = i.value;
  });
  const extra = document.getElementById('extra-params').value;
  if (extra) {
    try { Object.assign(params, JSON.parse(extra)); } catch(e) { alert('Invalid JSON in Extra'); return; }
  }
  document.getElementById('invoke-result').textContent = 'Invoking...';
  const res = await fetch('/api/invoke', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({name, params})});
  const j = await res.json();
  document.getElementById('invoke-result').textContent = JSON.stringify(j, null, 2);
}

async function loadConfig() {
  const res = await fetch('/api/config?path=json');
  const j = await res.json();
  document.getElementById('config-editor').value = j.content;
}

async function saveConfig() {
  const content = document.getElementById('config-editor').value;
  const res = await fetch('/api/config', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({path:'json', content})});
  const j = await res.json();
  if (j.status === 'ok') {
    alert('Saved and reloaded');
    await loadTools();
  } else {
    alert('Save failed: ' + j.error);
  }
}

window.addEventListener('load', async () => {
  await loadTools();
  await loadConfig();
  document.getElementById('invoke-tool').addEventListener('change', renderParams);
  document.getElementById('invoke-btn').addEventListener('click', invokeTool);
  document.getElementById('search-btn').addEventListener('click', invokeSearch);
  document.getElementById('save-config').addEventListener('click', saveConfig);
  setInterval(loadTools, 8000);
});

async function invokeSearch() {
  const q = document.getElementById('search-query').value;
  if (!q) return alert('Enter a query');
  document.getElementById('search-results').innerHTML = 'Searching...';
  const res = await fetch('/api/invoke', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({name:'research_search', params:{query:q, top_k:10}})});
  const j = await res.json();
  const area = document.getElementById('search-results');
  if (j.status === 'ok') {
    const results = j.result || j;
    if (results.error) {
      area.textContent = 'Error: ' + results.error;
      return;
    }
    const arr = results.results || [];
    if (!arr.length) {
      area.textContent = 'No results found.';
      return;
    }
    area.innerHTML = '';
    arr.forEach(r => {
      const d = document.createElement('div');
      d.className = 'search-hit';
      d.innerHTML = `<strong>${r.file}</strong> (score: ${r.score})<div>${escapeHtml(r.snippet)}</div>`;
      area.appendChild(d);
    });
  } else {
    area.textContent = 'Search failed: ' + (j.error || JSON.stringify(j));
  }
}

function escapeHtml(s) {
  if (!s) return '';
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}
