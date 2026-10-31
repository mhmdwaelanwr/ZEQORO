(() => {
  const dateInput = document.querySelector('#day');
  if (!dateInput) return;
  const slots = document.querySelector('#slots');
  const hidden = document.querySelector('#selected-slot');
  const button = document.querySelector('#confirm-btn');
  const note = document.querySelector('#selection-note');
  const eventSlug = window.location.pathname.split('/').pop();
  let sequence = 0;
  const localZone = Intl.DateTimeFormat().resolvedOptions().timeZone;
  document.querySelector('#timezone-current').textContent = localZone ? `Your device: ${localZone}` : '';
  async function refresh() {
    const current = ++sequence;
    hidden.value = '';
    button.disabled = true;
    note.textContent = 'Choose a time above to continue.';
    slots.innerHTML = '<span class="loading">Loading available times…</span>';
    try {
      const resp = await fetch(`/api/slots/${encodeURIComponent(eventSlug)}?day=${encodeURIComponent(dateInput.value)}`);
      if (!resp.ok) throw new Error('Unable to load availability');
      const data = await resp.json();
      if (current !== sequence) return;
      slots.replaceChildren();
      if (!data.slots.length) {
        slots.innerHTML = '<div class="no-slots">No times available for this day. Try another date.</div>';
        return;
      }
      for (const slot of data.slots) {
        const item = document.createElement('button');
        item.type = 'button';
        item.className = 'slot-btn';
        item.textContent = slot.label;
        item.addEventListener('click', () => {
          slots.querySelectorAll('.slot-btn').forEach(el => el.classList.remove('chosen'));
          item.classList.add('chosen');
          hidden.value = slot.start;
          note.textContent = `${dateInput.value} · ${slot.label} (${data.timezone})`;
          button.disabled = false;
        });
        slots.appendChild(item);
      }
    } catch (error) {
      if (current === sequence) slots.innerHTML = '<div class="no-slots">Could not load times. Refresh to retry.</div>';
    }
  }
  dateInput.addEventListener('change', refresh);
  refresh();
})();
