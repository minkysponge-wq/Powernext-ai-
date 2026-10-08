(() => {
  const table = document.querySelector('.entry-table');
  const rulesNode = document.querySelector('#station-live-rules');
  if (!table || !rulesNode) return;
  const rules = JSON.parse(rulesNode.textContent);
  const rows = [...table.querySelectorAll('tbody tr[data-reading-key]')];
  const fields = rows.map(row => ({row, key:row.dataset.readingKey, page:row.dataset.readingPage,
    value:row.querySelector('.entry-value textarea,.entry-value input'), unit:row.querySelector('.entry-unit input'),
    status:row.querySelector('.entry-status select'), check:row.querySelector('.live-check')}));
  const matches = (pattern,key) => new RegExp('^' + pattern.replace(/[.*+?^${}()|[\]\\]/g,'\\$&').replaceAll('\\*','.*') + '$').test(key);
  const canonicalUnit = value => ({'°c':'c','degc':'c','degreesc':'c','seconds':'s','second':'s','sec':'s','watts':'w','watt':'w','ohms':'ohm'}[value?.trim().replaceAll(' ','').toLowerCase()] || value?.trim().replaceAll(' ','').toLowerCase() || '');
  const number = field => { const raw = field.value?.value.trim() || ''; return raw && Number.isFinite(Number(raw)) ? Number(raw) : null; };
  const warning = (field,message) => { field.check.textContent = message; field.check.classList.add('check-warning'); };
  const check = () => {
    fields.forEach(field => { field.check.textContent = ''; field.check.classList.remove('check-warning'); });
    for (const rule of rules) {
      if (rule.type === 'range' || rule.type === 'unit') {
        fields.filter(field => matches(rule.field, field.key) && field.status?.value !== 'not_applicable').forEach(field => {
          if (!field.value?.value.trim()) return;
          if (rule.type === 'unit' && !rule.allowed.some(unit => canonicalUnit(unit) === canonicalUnit(field.unit?.value)))
            warning(field, 'Unit needs checking');
          if (rule.type === 'range') { const value = number(field); if (value === null || rule.min !== undefined && value < Number(rule.min) || rule.max !== undefined && value > Number(rule.max) || rule.unit && canonicalUnit(field.unit?.value) !== canonicalUnit(rule.unit)) warning(field,'Outside configured range or unit'); }
        });
        continue;
      }
      const groups = new Map();
      fields.forEach(field => { const parts = field.key.match(/^(.*)\.(\d+)\.([^.]+)$/); if (!parts) return;
        const id = field.page + ':' + parts[1] + ':' + parts[2];
        if (!groups.has(id)) groups.set(id, new Map()); groups.get(id).set(parts[3],field);
      });
      for (const group of groups.values()) {
        const members = [...rule.of,rule.equals].map(key => group.get(key));
        if (members.some(field => !field || number(field) === null)) continue;
        const values = members.map(number);
        const expected = rule.type === 'mean' ? values.slice(0,-1).reduce((a,b) => a+b,0)/(values.length-1)
          : rule.type === 'sum' ? values.slice(0,-1).reduce((a,b) => a+b,0)
          : rule.absolute ? Math.abs(values[1]-values[0]) : values[1]-values[0];
        const precision = members.map(field => { const decimals=(field.value.value.split('.')[1] || '').length; return 0.5 * 10**(-decimals); });
        const tolerance = rule.tol !== undefined ? Number(rule.tol) : (rule.type === 'mean'
          ? precision.slice(0,-1).reduce((a,b) => a+b,0)/(precision.length-1) : precision.slice(0,-1).reduce((a,b) => a+b,0)) + precision.at(-1);
        if (Math.abs(expected-values.at(-1)) > tolerance + 1e-9)
          warning(members.at(-1), `${rule.type === 'mean' ? 'Average' : rule.type === 'sum' ? 'Total' : 'Difference'} does not match inputs`);
      }
    }
  };
  table.addEventListener('input',check);
  table.addEventListener('change',check);
  check();
  const form = document.querySelector('#station-form');
  const saveState = document.querySelector('#station-save-state');
  let timer, pending = null, editNumber = 0, savedEdit = 0, leaving = false;
  const autosave = async () => {
    if (pending || leaving) return;
    const thisEdit = editNumber;
    saveState.textContent = 'Saving readings…';
    const data = new FormData(form);
    data.set('action','save');
    pending = fetch(location.href, {method:'POST',body:data,credentials:'same-origin',headers:{'X-Requested-With':'XMLHttpRequest'}});
    try {
      const response = await pending;
      if (!response.ok) {
        let message = response.status === 409 ? 'Another edit was saved. Reload before continuing.' : 'Could not save. Check the highlighted entries.';
        if (response.status === 400) {
          const details = await response.json();
          const first = (details.errors || []).find(errors => Object.keys(errors).length);
          if (first) message = Object.values(first).flat()[0] || message;
        }
        throw new Error(message);
      }
      const result = await response.json();
      form.querySelectorAll('input[name$="-id"]').forEach(input => {
        const version = form.querySelector(`[name="${input.name.slice(0,-3)}-version"]`);
        if (version && result.versions[input.value] !== undefined) version.value = result.versions[input.value];
      });
      savedEdit = thisEdit;
      saveState.textContent = thisEdit === editNumber ? 'All changes saved' : 'Saving latest changes…';
    } catch (error) { saveState.textContent = error.message + ' Use Save this page.'; }
    finally { pending = null; if (thisEdit !== editNumber && !leaving) timer = setTimeout(autosave, 300); }
  };
  const changed = () => { editNumber++; saveState.textContent = 'Unsaved changes'; clearTimeout(timer); timer = setTimeout(autosave, 1200); };
  form.addEventListener('input', changed);
  form.addEventListener('change', changed);
  form.addEventListener('submit', event => {
    clearTimeout(timer);
    if (pending) { event.preventDefault(); const button = event.submitter; const resume = () => { if (pending) setTimeout(resume,50); else { leaving = true; form.requestSubmit(button); } }; resume(); }
    else leaving = true;
  });
  window.addEventListener('beforeunload',event => {
    if (!leaving && (pending || editNumber > savedEdit)) { event.preventDefault(); event.returnValue = ''; }
  });
})();
