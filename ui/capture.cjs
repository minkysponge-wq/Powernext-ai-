/* Read-only visual audit. Run against a locally seeded and issued synthetic job. */
const {chromium} = require('playwright');
const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');

const root = path.resolve(__dirname, '..');
const base = process.env.VECTORLAB_TEST_URL || 'http://127.0.0.1:8009';
const access = JSON.parse(fs.readFileSync(process.env.VECTORLAB_ACCESS_FILE || path.join(root, 'output/walkthrough-demo-access.json')));
const users = JSON.parse(fs.readFileSync(process.env.VECTORLAB_USER_FILE || path.join(root, 'output/demo-users.json')));
const run = JSON.parse(fs.readFileSync(process.env.VECTORLAB_RUN_FILE || path.join(root, 'demo/final-walkthrough/results.json')));
const target = process.argv[2] || 'before';
const out = path.join(root, 'ui', target);
fs.mkdirSync(out, {recursive: true});

function code(uri) {
  const alphabet = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ234567';
  let bits = 0, buffer = 0;
  const bytes = [];
  for (const char of new URL(uri).searchParams.get('secret').toUpperCase().replace(/=+$/, '')) {
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
  const browser = await chromium.launch({headless: true,
    executablePath: process.env.VECTORLAB_BROWSER || 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe'});
  const rows = [];
  const job = access.job.id, report = run.reportId;
  async function capture(page, role, label, url) {
    const response = await page.goto(base + url);
    await page.waitForLoadState('networkidle');
    for (const [width, height] of [[1366, 768], [1920, 1080]]) {
      await page.setViewportSize({width, height});
      const file = `${role}-${label}-${width}x${height}.png`;
      await page.screenshot({path: path.join(out, file), fullPage: true});
      const metrics = await page.evaluate(() => {
        const rgb = value => {
          const parts = value.match(/[\d.]+/g);
          return parts && parts.length >= 3 ? parts.slice(0, 3).map(Number) : null;
        };
        const luminance = color => {
          const c = color.map(v => v / 255).map(v => v <= .04045 ? v / 12.92 : ((v + .055) / 1.055) ** 2.4);
          return .2126 * c[0] + .7152 * c[1] + .0722 * c[2];
        };
        const lowContrast = new Map();
        for (const el of document.querySelectorAll('body *')) {
          if (!Array.from(el.childNodes).some(node => node.nodeType === 3 && node.textContent.trim())) continue;
          const style = getComputedStyle(el);
          if (style.display === 'none' || style.visibility !== 'visible' || el.getBoundingClientRect().width < 1 || el.matches(':disabled')) continue;
          const foreground = rgb(style.color);
          let background = null, ancestor = el;
          while (ancestor && !background) {
            const raw = getComputedStyle(ancestor).backgroundColor;
            if (raw && !raw.endsWith(', 0)') && raw !== 'transparent') background = rgb(raw);
            ancestor = ancestor.parentElement;
          }
          background ||= [255, 255, 255];
          if (!foreground) continue;
          const light = Math.max(luminance(foreground), luminance(background));
          const dark = Math.min(luminance(foreground), luminance(background));
          const ratio = (light + .05) / (dark + .05);
          const size = parseFloat(style.fontSize), weight = parseInt(style.fontWeight, 10) || 400;
          const threshold = size >= 24 || (size >= 18.66 && weight >= 700) ? 3 : 4.5;
          if (ratio >= threshold) continue;
          const label = Array.from(el.childNodes).filter(node => node.nodeType === 3).map(node => node.textContent.trim()).join(' ').slice(0, 60);
          const key = `${style.color}/${background.join(',')}/${el.className}`;
          const item = lowContrast.get(key) || {tag: el.tagName.toLowerCase(), class: String(el.className).slice(0, 60), text: label, ratio: +ratio.toFixed(2), count: 0};
          item.count++;
          lowContrast.set(key, item);
        }
        const overflowing = [...document.querySelectorAll('body *')].filter(el => {
          const s = getComputedStyle(el);
          const box = el.getBoundingClientRect();
          return box.width > 30 && box.height > 12 && s.visibility !== 'hidden' &&
            s.display !== 'none' && el.scrollWidth > el.clientWidth + 4 &&
            s.overflowX !== 'auto' && s.overflowX !== 'scroll';
        }).slice(0, 12).map(el => ({tag: el.tagName.toLowerCase(), class: String(el.className).slice(0, 70),
          text: (el.innerText || '').trim().slice(0, 75), extra: el.scrollWidth - el.clientWidth}));
        return {pageOverflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
          candidates: overflowing, lowContrast: [...lowContrast.values()].sort((a, b) => b.count - a.count).slice(0, 20)};
      });
      rows.push({role, label, url, width, height, status: response.status(), file, ...metrics});
      fs.writeFileSync(path.join(out, 'audit.json'), JSON.stringify(rows, null, 2));
    }
  }
  async function login(role) {
    const context = await browser.newContext({viewport: {width: 1366, height: 768}});
    const page = await context.newPage();
    await page.goto(base + '/accounts/login/');
    await page.locator('[name="username"]').fill(access.users[role]);
    await page.locator('[name="password"]').fill(access.password);
    await page.locator('button').filter({hasText: 'Sign in'}).click();
    await page.waitForLoadState('networkidle');
    let tour = page.locator('#role-tour[open]');
    if (await tour.count()) await tour.locator('#tour-close').click();
    if (page.url().includes('/accounts/otp/')) {
      for (let attempt = 0; attempt < 2; attempt++) {
        await page.locator('[name="token"]').fill(code(users.totp_enrollment[role]));
        await page.locator('button').filter({hasText: 'Verify'}).click();
        await page.waitForLoadState('networkidle');
        if (!page.url().includes('/accounts/otp/')) break;
        await page.waitForTimeout(31000 - (Date.now() % 30000));
      }
      if (page.url().includes('/accounts/otp/')) throw new Error(`MFA failed for ${role}`);
    }
    tour = page.locator('#role-tour[open]');
    if (await tour.count()) await tour.locator('#tour-close').click();
    return {context, page};
  }
  const customer = await login('customer');
  await capture(customer.page, 'customer', 'home', '/customer/');
  await capture(customer.page, 'customer', 'new-request', '/customer/requests/new/');
  await capture(customer.page, 'customer', 'status', `/customer/requests/${access.job.file_number}/`);
  await customer.context.close();

  const admin = await login('admin');
  await capture(admin.page, 'admin', 'dashboard', '/');
  await capture(admin.page, 'admin', 'job', `/jobs/${job}/?step=report`);
  await capture(admin.page, 'admin', 'import', `/jobs/${job}/import/`);
  await capture(admin.page, 'admin', 'mapping', `/jobs/${job}/mapping/`);
  await capture(admin.page, 'admin', 'archive', '/reports/');
  await capture(admin.page, 'admin', 'audit', `/jobs/${job}/audit/`);
  await admin.context.close();

  const station = await login('engineer_a');
  await capture(station.page, 'station', 'job-tests', `/jobs/${job}/?step=tests`);
  // The issued fixture is locked; capture the active entry form via capture_station.cjs.
  await capture(station.page, 'station', 'report', `/reports/${report}/`);
  await station.context.close();

  const quality = await login('quality');
  await capture(quality.page, 'quality', 'queue', '/review/queue/');
  await capture(quality.page, 'quality', 'report', `/reports/${report}/`);
  await quality.context.close();

  const hod = await login('hod');
  await capture(hod.page, 'hod', 'queue', '/review/queue/');
  await capture(hod.page, 'hod', 'report', `/reports/${report}/`);
  await capture(hod.page, 'hod', 'delivery', `/reports/${report}/delivery/`);
  await hod.page.goto(`${base}/reports/${report}/`);
  const verify = await hod.page.locator('a').filter({hasText: 'Verify issued PDF'}).first().getAttribute('href');
  await hod.context.close();
  const publicContext = await browser.newContext({viewport: {width: 1366, height: 768}});
  const publicPage = await publicContext.newPage();
  await capture(publicPage, 'public', 'verification', verify);
  await publicContext.close();
  await browser.close();
  fs.writeFileSync(path.join(out, 'audit.json'), JSON.stringify(rows, null, 2));
  console.log(`Captured ${rows.length} screenshots; ${rows.filter(x => x.pageOverflow > 4).length} pages with horizontal overflow.`);
})().catch(error => {console.error(error.stack || String(error)); process.exit(1)});
