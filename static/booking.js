(() => {
  const dateInput = document.querySelector('#day');
  if (!dateInput) return;
  const slots = document.querySelector('#slots');
  const hidden = document.querySelector('#selected-slot');
  const confirmButton = document.querySelector('#confirm-btn');
  const continueButton = document.querySelector('#continue-btn');
  const details = document.querySelector('#guest-details');
  const note = document.querySelector('#selection-note');
  const tiles = document.querySelector('#date-tiles');
  const monthLabel = document.querySelector('#month-label');
  const prev = document.querySelector('#prev-dates');
  const next = document.querySelector('#next-dates');
  if (!slots || !hidden || !confirmButton) return;
  const url = dateInput.dataset.slotsUrl || `/api/slots/${encodeURIComponent(dateInput.dataset.eventSlug)}`;
  const localZone = Intl.DateTimeFormat().resolvedOptions().timeZone;
  const localFormatter = new Intl.DateTimeFormat(undefined, {hour: 'numeric', minute: '2-digit', timeZone: localZone || 'UTC'});
  const dateFormatter = new Intl.DateTimeFormat('en', {weekday: 'short', timeZone: 'UTC'});
  const monthFormatter = new Intl.DateTimeFormat('en', {month: 'long', year: 'numeric', timeZone: 'UTC'});
  const parseDay = (value) => new Date(`${value}T12:00:00Z`);
  const isoDay = (d) => d.toISOString().slice(0, 10);
  const shiftDay = (value, amount) => {const d = parseDay(value); d.setUTCDate(d.getUTCDate() + amount); return isoDay(d);};
  const earliest = dateInput.min;
  const latest = dateInput.max;
  let windowStart = dateInput.value;
  let sequence = 0;
  const currentTimeZone = document.querySelector('#timezone-current');
  if (currentTimeZone) currentTimeZone.textContent = localZone ? `Your device: ${localZone}` : '';

  function renderDates() {
    if (!tiles) return;
    tiles.replaceChildren();
    const first = parseDay(windowStart);
    if (monthLabel) monthLabel.textContent = monthFormatter.format(first);
    for (let i = 0; i < 6; i++) {
      const day = shiftDay(windowStart, i);
      const item = document.createElement('button');
      item.type = 'button';
      item.className = 'date-tile';
      item.disabled = day < earliest || day > latest;
      item.setAttribute('aria-pressed', String(day === dateInput.value));
      const weekday = document.createElement('span');
      weekday.textContent = dateFormatter.format(parseDay(day));
      const number = document.createElement('strong');
      number.textContent = day.slice(8, 10).replace(/^0/, '');
      item.append(weekday, number);
      item.setAttribute('aria-label', day);
      item.addEventListener('click', () => {dateInput.value = day; dateInput.dispatchEvent(new Event('change', {bubbles: true}));});
      tiles.appendChild(item);
    }
    if (prev) prev.disabled = windowStart <= earliest;
    if (next) next.disabled = shiftDay(windowStart, 6) > latest;
  }
  function resetSelection() {
    hidden.value = '';
    confirmButton.disabled = true;
    if (continueButton) {continueButton.disabled = true; continueButton.hidden = false;}
    if (details) details.hidden = true;
    if (note) note.textContent = 'Choose a time above to continue.';
  }
  async function refresh() {
    const current = ++sequence;
    resetSelection();
    renderDates();
    slots.replaceChildren();
    const loading = document.createElement('span');
    loading.className = 'loading';
    loading.textContent = 'Loading available times…';
    slots.appendChild(loading);
    try {
      const resp = await fetch(`${url}?day=${encodeURIComponent(dateInput.value)}`);
      if (!resp.ok) throw new Error('Unable to load availability');
      const data = await resp.json();
      if (current !== sequence) return;
      slots.replaceChildren();
      if (!data.slots.length) {
        const empty = document.createElement('div');
        empty.className = 'no-slots';
        empty.textContent = 'No times available for this day. Try another date.';
        slots.appendChild(empty);
        return;
      }
      for (const slot of data.slots) {
        const item = document.createElement('button');
        item.type = 'button';
        item.className = 'slot-btn';
        item.setAttribute('aria-pressed', 'false');
        const localLabel = localFormatter.format(new Date(slot.start));
        const primaryLabel = document.createElement('strong');
        primaryLabel.textContent = slot.label;
        item.appendChild(primaryLabel);
        if (localZone && localZone !== data.timezone) {
          const secondaryLabel = document.createElement('small');
          secondaryLabel.textContent = `${localLabel} local`;
          item.appendChild(secondaryLabel);
        }
        item.setAttribute('aria-label', localZone && localZone !== data.timezone ? `${slot.label} host time, ${localLabel} your local time` : slot.label);
        item.addEventListener('click', () => {
          slots.querySelectorAll('.slot-btn').forEach(el => { el.classList.remove('chosen'); el.setAttribute('aria-pressed', 'false'); });
          item.classList.add('chosen');
          item.setAttribute('aria-pressed', 'true');
          hidden.value = slot.start;
          if (note) note.textContent = `${dateInput.value} · ${slot.label} (${data.timezone})${localZone && localZone !== data.timezone ? ' / ' + localLabel + ' (' + localZone + ')' : ''}`;
          if (continueButton) continueButton.disabled = false;
          confirmButton.disabled = false;
        });
        slots.appendChild(item);
      }
    } catch (error) {
      if (current === sequence) {
        slots.replaceChildren();
        const failed = document.createElement('div');
        failed.className = 'no-slots';
        failed.textContent = 'Could not load availability. ';
        const retry = document.createElement('button');
        retry.type = 'button'; retry.className = 'link-button'; retry.textContent = 'Retry loading times';
        retry.addEventListener('click', refresh);
        failed.appendChild(retry);
        slots.appendChild(failed);
      }
    }
  }
  dateInput.addEventListener('change', () => {
    if (dateInput.value < windowStart || dateInput.value > shiftDay(windowStart, 5)) windowStart = dateInput.value;
    refresh();
  });
  prev?.addEventListener('click', () => {windowStart = shiftDay(windowStart, -6); if (windowStart < earliest) windowStart = earliest; dateInput.value = windowStart; dateInput.dispatchEvent(new Event('change', {bubbles: true}));});
  next?.addEventListener('click', () => {windowStart = shiftDay(windowStart, 6); dateInput.value = windowStart; dateInput.dispatchEvent(new Event('change', {bubbles: true}));});
  continueButton?.addEventListener('click', () => {
    if (!hidden.value) return;
    if (details) details.hidden = false;
    if (continueButton) continueButton.hidden = true;
    document.querySelector('#guest-name')?.focus();
  });
  refresh();
})();
