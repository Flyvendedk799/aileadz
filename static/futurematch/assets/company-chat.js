(function () {
  'use strict';
  const root = document.getElementById('companyChat');
  if (!root) return;
  const base = root.dataset.url;
  const people = [...root.querySelectorAll('.col-person')];
  const title = document.getElementById('colTitle');
  const messages = document.getElementById('colMessages');
  const form = document.getElementById('colForm');
  const body = document.getElementById('colBody');
  const send = document.getElementById('colSend');
  const status = document.getElementById('colStatus');
  let selected = null;
  let lastId = null;

  async function refresh() {
    if (!selected) return;
    const selectedAtStart = selected;
    try {
      const res = await fetch(base + '?format=json&recipient_id=' + encodeURIComponent(selected));
      if (!res.ok) throw new Error('Kunne ikke hente beskeder.');
      const data = await res.json();
      if (selected !== selectedAtStart) return;
      const nextId = data.messages.length ? data.messages[data.messages.length - 1].id : 0;
      if (lastId === nextId) return;
      lastId = nextId;
      messages.replaceChildren();
      for (const item of data.messages) {
        const box = document.createElement('div');
        box.className = 'col-message' + (item.mine ? ' mine' : '');
        box.textContent = item.body;
        const time = document.createElement('time');
        time.dateTime = item.created_at;
        time.textContent = new Date(item.created_at).toLocaleString('da-DK');
        box.appendChild(time);
        messages.appendChild(box);
      }
      messages.scrollTop = messages.scrollHeight;
      status.textContent = '';
    } catch (err) { status.textContent = err.message; }
  }

  for (const person of people) person.addEventListener('click', () => {
    selected = person.dataset.id;
    lastId = null;
    for (const other of people) other.classList.toggle('active', other === person);
    title.textContent = person.dataset.name;
    messages.replaceChildren();
    body.disabled = send.disabled = false;
    body.focus();
    refresh();
  });

  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const content = body.value.trim();
    if (!selected || !content) return;
    send.disabled = true;
    try {
      const res = await fetch(base, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ recipient_id: selected, body: content })
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || 'Beskeden kunne ikke sendes.');
      body.value = '';
      status.textContent = '';
      await refresh();
    } catch (err) { status.textContent = err.message; }
    finally { send.disabled = false; }
  });
  setInterval(refresh, 10000);
})();
