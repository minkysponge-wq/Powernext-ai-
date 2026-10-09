/* Capture an active, seeded station form before and after the CSS overlay. */
const {chromium} = require('playwright');
const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');

const root = path.resolve(__dirname, '..');
const access = JSON.parse(fs.readFileSync(process.env.VECTORLAB_ACCESS_FILE || path.join(root, 'output/walkthrough-demo-access.json')));
const users = JSON.parse(fs.readFileSync(process.env.VECTORLAB_USER_FILE || path.join(root, 'output/demo-users.json')));
const base = process.env.VECTORLAB_TEST_URL || 'http://127.0.0.1:8010';
function code(uri) {
  const alphabet = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ234567';
  const secret = new URL(uri).searchParams.get('secret');
  let bits = 0, buffer = 0;
  const bytes = [];
  for (const char of secret.toUpperCase().replace(/=+$/, '')) {
    buffer = (buffer << 5) | alphabet.indexOf(char);
    bits += 5;
    if (bits >= 8) { bits -= 8; bytes.push((buffer >> bits) & 255); }
  }
  const counter = Buffer.alloc(8);
  counter.writeBigUInt64BE(BigInt(Math.floor(Date.now() / 30000)));
  const digest = crypto.createHmac('sha1', Buffer.from(bytes)).update(counter).digest();
  return String((digest.readUInt32BE(digest.at(-1) & 15) & 0x7fffffff) % 1000000).padStart(6, '0');
}
(async () => {
  const browser = await chromium.launch({headless: true, executablePath: process.env.VECTORLAB_BROWSER || 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe'});
  const page = await browser.newPage({viewport: {width: 1366, height: 768}});
  await page.goto(base + '/accounts/login/');
  await page.locator('[name="username"]').fill(access.users.engineer_a);
  await page.locator('[name="password"]').fill(access.password);
  await page.getByRole('button', {name: 'Sign in'}).click();
  await page.waitForLoadState('networkidle');
  if (await page.locator('#role-tour[open]').count()) await page.locator('#tour-close').click();
  if (page.url().includes('/accounts/otp/')) {
    for (let attempt = 0; attempt < 2; attempt++) {
      await page.locator('[name="token"]').fill(code(users.totp_enrollment.engineer_a));
      await page.getByRole('button', {name: 'Verify'}).click();
      await page.waitForLoadState('networkidle');
      if (!page.url().includes('/accounts/otp/')) break;
      await page.waitForTimeout(31000 - (Date.now() % 30000));
    }
  }
  if (await page.locator('#role-tour[open]').count()) await page.locator('#tour-close').click();
  const response = await page.goto(`${base}/jobs/${access.job.id}/digital-review/?test=routine_test`);
  if (response.status() !== 200 || !(await page.locator('#station-form').count())) throw new Error('Active station form unavailable');
  const rows = [];
  for (const [width, height] of [[1366, 768], [1920, 1080]]) {
    await page.setViewportSize({width, height});
    await page.evaluate(() => document.querySelector('link[href*="ui-polish.css"]').disabled = true);
    await page.screenshot({path: path.join(root, 'ui/before', `station-entry-${width}x${height}.png`), fullPage: true});
    rows.push({version: 'before', width, height, status: response.status(), pageOverflow: await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)});
    await page.evaluate(() => document.querySelector('link[href*="ui-polish.css"]').disabled = false);
    await page.screenshot({path: path.join(root, 'ui/after', `station-entry-${width}x${height}.png`), fullPage: true});
    rows.push({version: 'after', width, height, status: response.status(), pageOverflow: await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)});
  }
  fs.writeFileSync(path.join(root, 'ui/station-audit.json'), JSON.stringify(rows, null, 2));
  await browser.close();
  console.log('Captured active station entry at both sizes.');
})().catch(error => { console.error(error.message); process.exit(1); });
