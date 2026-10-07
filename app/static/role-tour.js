(() => {
  const dialog = document.querySelector('#role-tour');
  if (!dialog || !dialog.showModal) return;
  const steps = {
    Customer: ['Submit a request in four short steps. Your unfinished details are saved privately.', 'Track each test and approval stage from My requests.', 'Download the approved report after it is issued.'],
    'Test Engineer': ['Today’s tests shows only your station assignments.', 'Enter readings in the station sheet and check live warnings against the instrument.', 'Lock a test only after every required reading has been reviewed.'],
    Quality: ['Open Review queue to see reports awaiting your decision.', 'Compare flagged checks and the PDF on one page.', 'Approve for HoD or send back with a recorded reason.'],
    HoD: ['Ready to sign shows Quality-approved reports.', 'Preview the complete PDF and verdicts before signing.', 'Your final issue action locks the revision and makes it available to the customer.'],
    Admin: ['Use Test jobs to monitor the lab pipeline.', 'Assign station work and configure mappings before a report is locked.', 'Review queue and audit history show pending decisions and changes.']
  };
  const roleSteps = steps[dialog.dataset.role] || steps.Admin;
  const key = `vectorlab-tour-${dialog.dataset.user}-${dialog.dataset.role}-v1`;
  let index = 0;
  const render = () => {
    document.querySelector('#tour-copy').textContent = roleSteps[index];
    document.querySelector('#tour-next').textContent = index === roleSteps.length - 1 ? 'Done' : 'Next';
  };
  const open = () => { index = 0; render(); dialog.showModal(); };
  document.querySelector('#tour-open')?.addEventListener('click',open);
  document.querySelector('#tour-next').addEventListener('click', () => {
    if (++index >= roleSteps.length) { localStorage.setItem(key,'seen'); dialog.close(); }
    else render();
  });
  document.querySelector('#tour-close').addEventListener('click', () => { localStorage.setItem(key,'seen'); dialog.close(); });
  if (!localStorage.getItem(key)) open();
})();
