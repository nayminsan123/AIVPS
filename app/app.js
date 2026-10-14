const $ = (selector) => document.querySelector(selector);
const login = $('#login');
const app = $('#app');
const messagesEl = $('#messages');
let history = [];

async function api(url, options = {}) {
  const response = await fetch(url, {
    credentials: 'same-origin',
    ...options,
    headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
  });
  if (!response.ok) {
    let data = {};
    try { data = await response.json(); } catch {}
    throw new Error(data.detail || `Request failed (${response.status})`);
  }
  return response;
}

function showMessage(role, text) {
  const element = document.createElement('div');
  element.className = `msg ${role}`;
  element.textContent = text;
  messagesEl.append(element);
  messagesEl.scrollTop = messagesEl.scrollHeight;
  return element;
}

async function loadUsers() {
  const container = $('#users-list');
  container.replaceChildren();
  const response = await api('/api/admin/users');
  const { users } = await response.json();
  for (const user of users) {
    const row = document.createElement('div');
    row.className = 'user-row';
    const name = document.createElement('strong');
    name.className = 'user-name';
    name.textContent = user.username;
    const role = document.createElement('select');
    role.className = 'small';
    for (const [value, label] of [['user', 'User'], ['admin', 'Admin']]) {
      const option = document.createElement('option');
      option.value = value;
      option.textContent = label;
      option.selected = user.role === value;
      role.append(option);
    }
    role.addEventListener('change', async () => {
      try { await api(`/api/admin/users/${user.id}`, { method: 'PATCH', body: JSON.stringify({ role: role.value }) }); await loadUsers(); }
      catch (error) { alert(error.message); await loadUsers(); }
    });
    const active = document.createElement('button');
    active.className = 'ghost small';
    active.textContent = user.active ? 'Disable' : 'Enable';
    active.addEventListener('click', async () => {
      try { await api(`/api/admin/users/${user.id}`, { method: 'PATCH', body: JSON.stringify({ active: !user.active }) }); await loadUsers(); }
      catch (error) { alert(error.message); }
    });
    const reset = document.createElement('button');
    reset.className = 'ghost small';
    reset.textContent = 'Reset password';
    reset.addEventListener('click', async () => {
      const password = prompt(`Enter a new password for ${user.username} (at least 12 characters):`);
      if (password === null) return;
      try { await api(`/api/admin/users/${user.id}`, { method: 'PATCH', body: JSON.stringify({ password }) }); alert('Password updated. Existing sessions for this user were signed out.'); }
      catch (error) { alert(error.message); }
    });
    const remove = document.createElement('button');
    remove.className = 'danger small';
    remove.textContent = 'Remove';
    remove.addEventListener('click', async () => {
      if (!confirm(`Remove ${user.username}? This cannot be undone.`)) return;
      try { await api(`/api/admin/users/${user.id}`, { method: 'DELETE' }); await loadUsers(); }
      catch (error) { alert(error.message); }
    });
    const status = document.createElement('span');
    status.className = 'badge';
    status.textContent = user.active ? 'Active' : 'Disabled';
    row.append(name, role, status, active, reset, remove);
    container.append(row);
  }
}

async function loadSettings() {
  const response = await api('/api/admin/settings');
  const { settings } = await response.json();
  $('#system-prompt').value = settings.system_prompt;
  $('#temperature').value = settings.temperature;
  $('#top-p').value = settings.top_p;
  $('#max-tokens').value = settings.max_tokens;
}

async function boot() {
  const response = await api('/api/session');
  const session = await response.json();
  $('#model-name').textContent = session.model;
  if (!session.authenticated) { login.classList.remove('hidden'); return; }
  $('#username').textContent = session.username;
  app.classList.remove('hidden');
  if (session.role === 'admin') {
    $('#tabs').classList.remove('hidden');
    try { await Promise.all([loadSettings(), loadUsers()]); }
    catch (error) { $('#settings-status').textContent = error.message; }
  }
}

$('#login-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  $('#login-error').textContent = '';
  const form = new FormData(event.currentTarget);
  try {
    await api('/api/login', { method: 'POST', body: JSON.stringify({ username: form.get('username'), password: form.get('password') }) });
    location.reload();
  } catch (error) { $('#login-error').textContent = error.message; }
});

$('#logout').addEventListener('click', async () => {
  try { await api('/api/logout', { method: 'POST', body: '{}' }); location.reload(); }
  catch (error) { alert(error.message); }
});

$('#tabs').addEventListener('click', (event) => {
  const button = event.target.closest('[data-panel]');
  if (!button) return;
  document.querySelectorAll('.tab').forEach((tab) => tab.classList.toggle('active', tab === button));
  document.querySelectorAll('.panel').forEach((panel) => panel.classList.toggle('hidden', panel.id !== button.dataset.panel));
});

$('#settings-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const status = $('#settings-status');
  status.className = 'status';
  try {
    await api('/api/admin/settings', { method: 'PUT', body: JSON.stringify({
      system_prompt: $('#system-prompt').value,
      temperature: Number($('#temperature').value),
      top_p: Number($('#top-p').value),
      max_tokens: Number($('#max-tokens').value),
    }) });
    status.textContent = 'Settings saved.';
  } catch (error) { status.className = 'status error'; status.textContent = error.message; }
});

$('#user-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const status = $('#user-status');
  status.className = 'status';
  const form = new FormData(event.currentTarget);
  try {
    await api('/api/admin/users', { method: 'POST', body: JSON.stringify({ username: form.get('username'), password: form.get('password'), role: form.get('role') }) });
    event.currentTarget.reset();
    status.textContent = 'User added.';
    await loadUsers();
  } catch (error) { status.className = 'status error'; status.textContent = error.message; }
});

$('#refresh-users').addEventListener('click', () => loadUsers().catch((error) => alert(error.message)));

$('#chat-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const input = $('#prompt');
  const text = input.value.trim();
  if (!text) return;
  input.value = '';
  history.push({ role: 'user', content: text });
  showMessage('user', text);
  const answer = showMessage('assistant', '');
  const send = $('#send');
  send.disabled = true;
  try {
    const response = await api('/api/chat', { method: 'POST', body: JSON.stringify({ messages: history }) });
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const frames = buffer.split('\n\n');
      buffer = frames.pop();
      for (const frame of frames) {
        const line = frame.split('\n').find((part) => part.startsWith('data:'));
        if (!line) continue;
        const data = line.slice(5).trim();
        if (data === '[DONE]') continue;
        if (data.startsWith('[ERROR]')) throw new Error(data.slice(7));
        try { answer.textContent += JSON.parse(data).choices?.[0]?.delta?.content || ''; } catch {}
      }
      messagesEl.scrollTop = messagesEl.scrollHeight;
    }
    history.push({ role: 'assistant', content: answer.textContent });
  } catch (error) { answer.textContent = error.message; }
  finally { send.disabled = false; input.focus(); }
});

boot().catch((error) => { login.classList.remove('hidden'); $('#login-error').textContent = error.message; });
