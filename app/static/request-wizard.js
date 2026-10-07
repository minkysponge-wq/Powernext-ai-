(() => {
  const form = document.querySelector('#request-form');
  if (!form) return;
  const steps = [...form.querySelectorAll('[data-wizard-step]')];
  const markers = [...document.querySelectorAll('.wizard-steps li')];
  const back = document.querySelector('#wizard-back');
  const next = document.querySelector('#wizard-next');
  const submit = document.querySelector('#wizard-submit');
  const status = document.querySelector('#draft-state');
  let current = form.querySelector('.errorlist') ? Math.max(0, steps.findIndex(step => step.querySelector('.errorlist'))) : 0;
  let saveTimer;
  let submitting = false;
  const show = index => {
    current = Math.max(0, Math.min(steps.length - 1, index));
    steps.forEach((step, i) => { step.hidden = i !== current; step.querySelectorAll('input,textarea,select').forEach(input => { if (input.type !== 'hidden') input.disabled = i !== current; }); });
    markers.forEach((marker, i) => { if (i === current) marker.setAttribute('aria-current', 'step'); else marker.removeAttribute('aria-current'); });
    back.hidden = current === 0; next.hidden = current === steps.length - 1; submit.hidden = current !== steps.length - 1;
    if (current === 3) {
      const summary = document.querySelector('#request-summary');
      const line = (label, value) => { const dt = document.createElement('dt'); dt.textContent = label; const dd = document.createElement('dd'); dd.textContent = value || 'Not entered'; summary.append(dt, dd); };
      summary.replaceChildren();
      ['customer','customer_address','manufacturer','sample_particulars'].forEach(name => line(name.replaceAll('_',' '), form.elements[name].value));
      line('Tests', [...form.querySelectorAll('[name="requested_tests"]:checked')].map(input => input.parentElement.textContent.trim()).join(', '));
    }
  };
  const stepValid = () => {
    const step = steps[current];
    if (current === 2 && !step.querySelector('[name="requested_tests"]:checked')) { status.textContent = 'Choose at least one test to continue.'; return false; }
    const invalid = [...step.querySelectorAll('input,textarea,select')].find(input => !input.checkValidity());
    if (invalid) { invalid.reportValidity(); return false; }
    return true;
  };
  back.addEventListener('click', () => show(current - 1));
  next.addEventListener('click', () => { if (stepValid()) show(current + 1); });
  form.addEventListener('submit', event => {
    clearTimeout(saveTimer);
    steps.forEach(step => step.querySelectorAll('input,textarea,select').forEach(input => { input.disabled = false; }));
    if (![0,1,2].every(index => { show(index); return stepValid(); })) { event.preventDefault(); return; }
    show(3);
    steps.forEach(step => step.querySelectorAll('input,textarea,select').forEach(input => { input.disabled = false; }));
    submitting = true;
  });
  const save = async () => {
    if (submitting) return;
    const data = new FormData(form);
    // Disabled wizard inputs are still part of the draft payload.
    steps.forEach(step => step.querySelectorAll('input,textarea,select').forEach(input => {
      if (!input.name || input.type === 'hidden') return;
      if (input.type === 'checkbox') { if (input.checked) data.append(input.name, input.value); }
      else data.set(input.name, input.value);
    }));
    try {
      const response = await fetch(document.querySelector('.request-wizard').dataset.draftUrl, { method:'POST', body:data, credentials:'same-origin' });
      status.textContent = response.ok ? 'Draft saved' : 'Draft not saved. Check your connection.';
    } catch { status.textContent = 'Draft not saved. Check your connection.'; }
  };
  form.addEventListener('input', () => { status.textContent = 'Saving draft…'; clearTimeout(saveTimer); saveTimer = setTimeout(save, 800); });
  form.addEventListener('change', () => { clearTimeout(saveTimer); saveTimer = setTimeout(save, 300); });
  document.querySelector('#reuse-details')?.addEventListener('click', () => {
    const details = JSON.parse(document.querySelector('#previous-request-details').textContent);
    Object.entries(details).forEach(([key,value]) => { form.elements[key].value = value; });
    status.textContent = 'Company details copied from your last request'; save();
  });
  show(current);
})();
