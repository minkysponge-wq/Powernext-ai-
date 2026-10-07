(() => {
  const jobRows = [...document.querySelectorAll('[data-job-stage]')];
  document.querySelectorAll('[data-job-filter]').forEach(button => button.addEventListener('click', () => {
    const selected = button.dataset.jobFilter;
    document.querySelectorAll('[data-job-filter]').forEach(item => item.setAttribute('aria-pressed', String(item === button)));
    jobRows.forEach(row => { row.hidden = selected !== 'all' && row.dataset.jobStage !== selected; });
    const count = jobRows.filter(row => !row.hidden).length;
    document.querySelector('#queue-count').textContent = count + ' jobs shown';
    document.querySelector('#filter-empty').hidden = count > 0 || jobRows.length === 0;
  }));
  const upload = document.querySelector('input[type=file]');
  if (upload) upload.addEventListener('change', () => {
    const label = document.querySelector('#selected-file');
    if (label) label.textContent = upload.files[0] ? 'Ready to upload: ' + upload.files[0].name : '';
  });
  document.querySelectorAll('form[data-confirm]').forEach(item => item.addEventListener('submit', event => {
    if (!window.confirm(item.dataset.confirm)) event.preventDefault();
  }));
  const form = document.querySelector('#review-form');
  if (!form) return;
  const keyboardHint = document.createElement('p');
  keyboardHint.className = 'review-keyboard-hint';
  keyboardHint.textContent = 'Keyboard: Enter confirms; Tab edits; J/K moves between readings outside an input (Alt+J/K while editing). Shift+Enter adds a line. Changes save automatically.';
  form.before(keyboardHint);
  let dirty = false;
  let editNumber = 0;
  let saveTimer, pending = null, leaving = false;
  const state = document.querySelector('#save-state');
  const rows = [...form.querySelectorAll('.review-row')];
  const progress = document.createElement('p');
  progress.className = 'review-progress';
  progress.setAttribute('role', 'status');
  document.querySelector('.review-tools')?.prepend(progress);
  const startedAt = performance.now();
  const initialDone = rows.filter(row => row.classList.contains('is-reviewed')).length;
  const activate = row => {
    if (!row) return;
    rows.forEach(item => { item.hidden = item !== row; });
    lastRow = row;
    showPage(row.dataset.sourcePage);
    row.querySelector('textarea')?.focus({preventScroll:true});
  };
  const updateRemaining = () => {
    const count = form.querySelectorAll('.needs-review').length;
    document.querySelector('#review-remaining').textContent = count ? count + ' unchecked on this page' : 'This page is reviewed';
    document.querySelector('#next-pending').disabled = count === 0;
    const done = rows.length - count;
    const seconds = (performance.now() - startedAt) / 1000;
    const rate = done > initialDone && seconds > 0 ? Math.round((seconds / (done - initialDone)) * count / 60) : null;
    progress.textContent = `${done} of ${rows.length} checked on this page` + (rate !== null && count ? ` · about ${Math.max(1, rate)} min left at your current pace` : '');
  };
  let lastRow = null;
  const focusNextPending = (afterRow = lastRow) => {
    const after = rows.indexOf(afterRow) + 1;
    const row = [...rows.slice(after), ...rows.slice(0, after)].find(item => item.classList.contains('needs-review'));
    if (row) activate(row);
  };
  document.addEventListener('keydown', event => {
    if (!['j','k'].includes(event.key.toLowerCase()) || event.ctrlKey || event.metaKey) return;
    const editable = event.target.matches('input,textarea,select,[contenteditable]');
    if (editable && !event.altKey) return;
    if (!rows.length) return;
    const current = Math.max(0, rows.indexOf(lastRow));
    const direction = event.key.toLowerCase() === 'j' ? 1 : -1;
    const next = rows[Math.min(rows.length - 1, Math.max(0, current + direction))];
    event.preventDefault();
    activate(next);
  });
  document.querySelector('#next-pending').addEventListener('click', () => focusNextPending());
  updateRemaining();
  activate(rows.find(row => row.classList.contains('needs-review')) || rows[0]);
  const autosave = async () => {
    if (pending || leaving || !dirty) return;
    const thisEdit = editNumber;
    state.textContent = 'Saving readings…';
    const data = new FormData(form);
    data.set('action','save');
    pending = fetch(location.href,{method:'POST',body:data,credentials:'same-origin',headers:{'X-Requested-With':'XMLHttpRequest'}});
    try {
      const response = await pending;
      if (!response.ok) throw new Error(response.status === 409 ? 'Another edit was saved. Reload before continuing.' : 'Could not save. Check the current reading.');
      const result = await response.json();
      form.querySelectorAll('input[name$="-id"]').forEach(input => {
        const version = form.querySelector(`[name="${input.name.slice(0,-3)}-version"]`);
        if (version && result.versions[input.value] !== undefined) version.value = result.versions[input.value];
      });
      dirty = thisEdit !== editNumber;
      state.textContent = dirty ? 'Saving latest changes…' : 'All displayed values are saved';
      state.style.color = '';
    } catch(error) { state.textContent = error.message + ' Use Save before leaving.'; }
    finally { pending = null; if (thisEdit !== editNumber && !leaving) saveTimer = setTimeout(autosave,300); }
  };
  const markDirty = () => { dirty = true; editNumber++; state.textContent = 'Unsaved changes'; state.style.color = '#a6792e'; clearTimeout(saveTimer); saveTimer = setTimeout(autosave,1200); };
  const updateRow = (row) => {
    const status = row.querySelector('select[name$="-status"]');
    const badge = row.querySelector('[data-status-label]');
    const reviewed = ['verified', 'not_applicable'].includes(status.value);
    row.classList.toggle('is-reviewed', reviewed);
    row.classList.toggle('needs-review', !reviewed);
    badge.className = 'badge ' + (reviewed ? 'green' : status.value === 'ambiguous' ? 'ambiguous' : 'amber');
    badge.textContent = status.options[status.selectedIndex].text;
    row.querySelector('.verify-button').textContent = status.value === 'verified' ? '✓ Confirmed' : '✓ Confirm value';
    updateRemaining();
  };
  let previousConfirmation = null;
  const undo = document.createElement('button');
  undo.type = 'button'; undo.className = 'secondary compact'; undo.textContent = 'Undo last confirmation'; undo.hidden = true;
  document.querySelector('.review-tools')?.append(undo);
  undo.addEventListener('click', () => {
    if (!previousConfirmation) return;
    previousConfirmation.row.querySelector('select[name$="-status"]').value = previousConfirmation.status;
    updateRow(previousConfirmation.row); markDirty(); activate(previousConfirmation.row);
    previousConfirmation = null; undo.hidden = true;
  });
  form.addEventListener('input', markDirty);
  form.addEventListener('change', (event) => {
    markDirty();
    const row = event.target.closest('.review-row');
    if (row) { row.querySelector('textarea').setCustomValidity(''); updateRow(row); }
  });
  form.addEventListener('click', (event) => {
    const row = event.target.closest('.review-row');
    if (!row) return;
    if (event.target.closest('.verify-button')) {
      const value = row.querySelector('textarea');
      if (!value.value.trim()) { value.focus(); value.setCustomValidity('Enter a reading, or choose Not applicable.'); value.reportValidity(); return; }
      value.setCustomValidity('');
      previousConfirmation = {row,status:row.querySelector('select[name$="-status"]').value};
      undo.hidden = false;
      row.querySelector('select[name$="-status"]').value = 'verified';
      updateRow(row); markDirty();
      event.target.textContent = '✓ Confirmed';
      focusNextPending(row);
    }
    if (event.target.closest('.view-page')) showPage(row.dataset.sourcePage);
  });
  form.addEventListener('keydown', (event) => {
    const row = event.target.closest('.review-row');
    if (!row) return;
    if (event.altKey && (event.key === 'ArrowDown' || event.key === 'ArrowUp')) {
      event.preventDefault();
      const direction = event.key === 'ArrowDown' ? 1 : -1;
      const next = rows[rows.indexOf(row) + direction];
      if (next) activate(next);
      return;
    }
    if (event.key === 'Enter' && !event.shiftKey && !event.ctrlKey && !event.altKey && event.target.matches('textarea')) {
      event.preventDefault();
      row.querySelector('.verify-button').click();
    }
  });
  function showPage(page) {
    if (!page) return;
    const preview = document.querySelector('#source-preview');
    const number = document.querySelector('#source-page-number');
    if (number.textContent !== page) { preview.src = preview.dataset.base.replace('/pages/1/', '/pages/' + page + '/'); number.textContent = page; }
  }
  form.addEventListener('focusin', (event) => {
    const row = event.target.closest('.review-row');
    if (row) {
      lastRow = row;
      showPage(row.dataset.sourcePage);
      const highlight = document.querySelector('#source-highlight');
      if (row.dataset.sourceBox) {
        const [left, top, right, bottom] = JSON.parse(row.dataset.sourceBox);
        Object.assign(highlight.style, {left:left*100+'%', top:top*100+'%', width:(right-left)*100+'%', height:(bottom-top)*100+'%'});
        highlight.hidden = false;
      } else highlight.hidden = true;
    }
    if (event.target.tagName === 'TEXTAREA') event.target.setCustomValidity('');
  });
  let zoom = 100;
  document.querySelectorAll('[data-zoom]').forEach(button => button.addEventListener('click', () => {
    zoom = button.dataset.zoom === 'reset' ? 100 : Math.max(100, Math.min(250, zoom + (button.dataset.zoom === 'in' ? 25 : -25)));
    document.querySelector('#source-canvas').style.width = zoom + '%';
    document.querySelector('#zoom-value').textContent = zoom + '%';
  }));
  form.addEventListener('submit', event => {
    clearTimeout(saveTimer);
    if (pending) { event.preventDefault(); const button = event.submitter; const resume = () => { if (pending) setTimeout(resume,50); else { leaving = true; form.requestSubmit(button); } }; resume(); }
    else { leaving = true; dirty = false; }
  });
  window.addEventListener('beforeunload', (event) => { if (dirty) { event.preventDefault(); event.returnValue = ''; } });
})();
