/* Verify that the same issued synthetic certificate downloads unchanged after UI polish. */
const {chromium} = require('playwright');
const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');

(async () => {
  const root = path.resolve(__dirname, '..');
  const access = JSON.parse(fs.readFileSync(path.join(root, 'output/walkthrough-demo-access.json')));
  const base = process.env.VECTORLAB_TEST_URL || 'http://127.0.0.1:8009';
  const browser = await chromium.launch({headless: true, executablePath: process.env.VECTORLAB_BROWSER || 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe'});
  const page = await browser.newPage({acceptDownloads: true});
  await page.goto(base + '/accounts/login/');
  await page.locator('[name="username"]').fill(access.users.customer);
  await page.locator('[name="password"]').fill(access.password);
  await page.getByRole('button', {name: 'Sign in'}).click();
  await page.goto(`${base}/customer/requests/${access.job.file_number}/`);
  const href = await page.getByRole('link', {name: 'Download approved PDF'}).getAttribute('href');
  const response = await page.context().request.get(new URL(href, base).href);
  assert.equal(response.status(), 200, 'Customer PDF download failed');
  const target = path.join(root, 'private/ui-polish-backup/after-issued.pdf');
  fs.writeFileSync(target, await response.body());
  const before = fs.readFileSync(path.join(root, 'demo/final-walkthrough/issued.pdf'));
  const after = fs.readFileSync(target);
  const sha = bytes => crypto.createHash('sha256').update(bytes).digest('hex');
  assert.deepEqual(after, before, 'Issued PDF bytes changed');
  fs.writeFileSync(path.join(root, 'ui/pdf-comparison.json'), JSON.stringify({beforeSha256: sha(before), afterSha256: sha(after), byteIdentical: true, bytes: before.length}, null, 2));
  await browser.close();
  console.log('Issued PDF bytes are identical:', sha(after));
})().catch(error => { console.error(error.message); process.exit(1); });
