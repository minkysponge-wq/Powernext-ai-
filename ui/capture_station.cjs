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
    await page.evaluate(() => {
      const combined = document.querySelector('link[href*="ui-polish.css"]');
      for (const name of ['app.css', 'lab-theme.css', 'interface-refresh.css', 'brand-refresh.css', 'workflow-ux.css']) {
        const link = document.createElement('link');
        link.rel = 'stylesheet';
        link.href = `/static/${name}`;
        link.dataset.baselineStyle = '1';
        document.head.append(link);
      }
      combined.disabled = true;
    });
    await page.waitForLoadState('networkidle');
    await page.screenshot({path: path.join(root, 'ui/before', `station-entry-${width}x${height}.png`), fullPage: true});
    rows.push({version: 'before', width, height, status: response.status(), pageOverflow: await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)});
    await page.evaluate(() => {
      document.querySelectorAll('[data-baseline-style="1"]').forEach(link => link.remove());
      document.querySelector('link[href*="ui-polish.css"]').disabled = false;
    });
    await page.screenshot({path: path.join(root, 'ui/after', `station-entry-${width}x${height}.png`), fullPage: true});
    const metrics = await page.evaluate(() => {
      const rgb = raw => (raw.match(/[\d.]+/g) || []).slice(0, 3).map(Number);
      const lum = c => {
        const v = c.map(x => x / 255).map(x => x <= .04045 ? x / 12.92 : ((x + .055) / 1.055) ** 2.4);
        return .2126 * v[0] + .7152 * v[1] + .0722 * v[2];
      };
      const low = [];
      for (const el of document.querySelectorAll('body *')) {
        const text = [...el.childNodes].filter(n => n.nodeType === 3).map(n => n.textContent.trim()).join(' ').trim();
        if (!text || el.getBoundingClientRect().width < 1) continue;
        const style = getComputedStyle(el);
        if (style.display === 'none' || style.visibility !== 'visible' || el.matches(':disabled')) continue;
        let ancestor = el, background;
        while (ancestor && !background) {
          const raw = getComputedStyle(ancestor).backgroundColor;
          if (raw && !raw.endsWith(', 0)') && raw !== 'transparent') background = rgb(raw);
          ancestor = ancestor.parentElement;
        }
        background ||= [255, 255, 255];
        const foreground = rgb(style.color);
        if (foreground.length !== 3 || background.length !== 3) continue;
        const ratio = (Math.max(lum(foreground), lum(background)) + .05) / (Math.min(lum(foreground), lum(background)) + .05);
        const size = parseFloat(style.fontSize), weight = parseInt(style.fontWeight, 10) || 400;
        if (ratio < (size >= 24 || size >= 18.66 && weight >= 700 ? 3 : 4.5)) low.push({text: text.slice(0, 60), ratio: +ratio.toFixed(2)});
      }
      return {pageOverflow: document.documentElement.scrollWidth - document.documentElement.clientWidth, lowContrast: low.slice(0, 20), lowContrastCount: low.length};
    });
    rows.push({version: 'after', width, height, status: response.status(), ...metrics});
  }
  fs.writeFileSync(path.join(root, 'ui/station-audit.json'), JSON.stringify(rows, null, 2));
  await browser.close();
  console.log('Captured active station entry at both sizes.');
})().catch(error => { console.error(error.message); process.exit(1); });
