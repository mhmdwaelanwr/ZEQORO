(() => {
  const notice = document.createElement('div');
  notice.className = 'copy-notice';
  notice.setAttribute('role', 'status');
  notice.setAttribute('aria-live', 'polite');
  notice.hidden = true;
  document.body.appendChild(notice);
  let hideTimeout;
  document.addEventListener('click', async (event) => {
    const button = event.target.closest('[data-copy-url]');
    if (!button) return;
    const url = button.dataset.copyUrl;
    if (!url || !/^https?:\/\//.test(url)) return;
    try {
      if (!navigator.clipboard?.writeText) throw new Error('Clipboard unavailable');
      await navigator.clipboard.writeText(url);
      notice.textContent = 'Booking link copied to clipboard';
    } catch (_) {
      notice.textContent = `Copy your link: ${url}`;
    }
    notice.hidden = false;
    clearTimeout(hideTimeout);
    hideTimeout = setTimeout(() => { notice.hidden = true; }, 4400);
  });
})();
