/* One synthetic job, seven station screens, three MFA approvals, signed PDF and QR.
   Run after the documented local seed. Access files stay in ignored output/. */
const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');

const root = path.resolve(__dirname, '..');
const base = process.env.VECTORLAB_TEST_URL || 'http://127.0.0.1:8000';
const access = JSON.parse(fs.readFileSync(process.env.VECTORLAB_ACCESS_FILE || path.join(root, 'output/walkthrough-demo-access.json')));
const userAccess = JSON.parse(fs.readFileSync(process.env.VECTORLAB_USER_FILE || path.join(root, 'output/demo-users.json')));
const evidenceDir = process.env.VECTORLAB_SCREENSHOT_DIR || path.join(root, 'demo/run');
fs.mkdirSync(evidenceDir, { recursive: true });

function base32(input) {
  const alphabet = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ234567';
  let bits = 0, buffer = 0;
  const bytes = [];
  for (const character of input.toUpperCase().replace(/=+$/, '')) {
    const value = alphabet.indexOf(character);
    if (value < 0) throw new Error('Invalid TOTP enrollment secret');
    buffer = (buffer << 5) | value;
    bits += 5;
    if (bits >= 8) { bits -= 8; bytes.push((buffer >> bits) & 255); }
  }
  return Buffer.from(bytes);
}
function totp(uri) {
  const secret = new URL(uri).searchParams.get('secret');
  if (!secret) throw new Error('MFA enrollment is missing');
  const counter = Buffer.alloc(8);
  counter.writeBigUInt64BE(BigInt(Math.floor(Date.now() / 30000)));
  const digest = crypto.createHmac('sha1', base32(secret)).update(counter).digest();
  const offset = digest.at(-1) & 15;
  return String((digest.readUInt32BE(offset) & 0x7fffffff) % 1000000).padStart(6, '0');
}

(async () => {
  const browser = await chromium.launch({headless: true,
    executablePath: process.env.VECTORLAB_BROWSER || 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe'});
  const contexts = [];
  const stages = {};
  const started = Date.now();
  let imageNumber = 0;
  async function screenshot(page, label) {
    const name = `${String(++imageNumber).padStart(2, '0')}-${label}.png`;
    await page.screenshot({path: path.join(evidenceDir, name), fullPage: true});
  }
  async function stage(name, action) {
    const start = Date.now();
    await action();
    stages[name] = Math.round((Date.now() - start) / 1000);
  }
  async function login(role) {
    const context = await browser.newContext({acceptDownloads: true});
    contexts.push(context);
    const page = await context.newPage();
    page.on('dialog', dialog => dialog.accept());
    await page.goto(base + '/accounts/login/');
    await page.locator('input[name="username"]').fill(access.users[role]);
    await page.locator('input[name="password"]').fill(access.password);
    await page.locator('button').filter({hasText: 'Sign in'}).click();
    await page.waitForLoadState('networkidle');
    let tour = page.locator('#role-tour[open]');
    if (await tour.count()) await tour.locator('#tour-close').click();
    if (page.url().includes('/accounts/otp/')) {
      await page.locator('input[name="token"]').fill(totp(userAccess.totp_enrollment[role]));
      await page.locator('button').filter({hasText: 'Verify'}).click();
      await page.waitForLoadState('networkidle');
      if (page.url().includes('/accounts/otp/')) {
        // The setup command may have just used this device. TOTP replay is
        // correctly refused; wait for the next authenticator time step.
        await page.waitForTimeout(31000 - (Date.now() % 30000));
        await page.locator('input[name="token"]').fill(totp(userAccess.totp_enrollment[role]));
        await page.locator('button').filter({hasText: 'Verify'}).click();
        await page.waitForLoadState('networkidle');
      }
      assert(!page.url().includes('/accounts/otp/'), `${role} MFA failed`);
    } else if (role !== 'customer') {
      throw new Error(`${role} reached the app without its mandatory MFA challenge`);
    }
    tour = page.locator('#role-tour[open]');
    if (await tour.count()) await tour.locator('#tour-close').click();
    return page;
  }
  try {
    let customer, engineerA, engineerB, admin, quality, hod, reportId, issued;
    const jobId = access.job.id;
    const stations = ['routine_test', 'short_circuit', 'loss_measurement', 'loss_calculation',
      'transformer_proforma', 'temperature_rise', 'pressure_oil_leakage'];
    await stage('customer_request', async () => {
      customer = await login('customer');
      await customer.goto(base + '/customer/requests/' + access.job.file_number + '/');
      assert((await customer.locator('body').innerText()).includes('SYNTHETIC WALKTHROUGH'));
      await screenshot(customer, 'customer-request-and-status');
    });
    await stage('station_entry_and_locks', async () => {
      engineerA = await login('engineer_a');
      engineerB = await login('engineer_b');
      for (let index = 0; index < stations.length; index++) {
        const page = index % 2 ? engineerB : engineerA;
        await page.goto(`${base}/jobs/${jobId}/digital-review/?test=${stations[index]}`);
        if (index === 0) await screenshot(page, 'station-reviewed-readings');
        const lock = page.locator('form[action$="/lock/"]');
        assert.equal(await lock.count(), 1, `Station ${stations[index]} is not ready to lock`);
        await lock.locator('[name="confirmed"]').check();
        await lock.locator('button').click();
        await page.waitForLoadState('networkidle');
        await screenshot(page, `station-${index + 1}-locked`);
      }
    });
    await stage('report_draft', async () => {
      admin = await login('admin');
      await admin.goto(`${base}/jobs/${jobId}/?step=report`);
      await screenshot(admin, 'report-ready');
      await admin.locator('form[action$="/report/"] button').click();
      await admin.waitForLoadState('networkidle');
      const match = admin.url().match(/\/reports\/([a-f0-9-]+)\/$/);
      assert(match, `Draft report did not open: ${admin.url()}`);
      reportId = match[1];
      assert((await admin.locator('body').innerText()).includes('23 pass'));
      await screenshot(admin, 'draft-23-rule-verdicts');
    });
    await stage('engineer_lock', async () => {
      await engineerA.goto(`${base}/reports/${reportId}/`);
      const form = engineerA.locator('form[action$="/engineer-lock/"]');
      assert.equal(await form.count(), 1);
      await form.locator('[name="confirmed"]').check();
      await form.locator('button').click();
      await engineerA.waitForLoadState('networkidle');
      await screenshot(engineerA, 'engineer-mfa-locked');
    });
    await stage('quality_verify', async () => {
      quality = await login('quality');
      await quality.goto(`${base}/reports/${reportId}/`);
      const form = quality.locator('form[action$="/quality-verify/"]');
      assert.equal(await form.count(), 1);
      await form.locator('[name="confirmed"]').check();
      await form.locator('button').click();
      await quality.waitForLoadState('networkidle');
      await screenshot(quality, 'quality-mfa-verified');
    });
    await stage('hod_issue', async () => {
      hod = await login('hod');
      await hod.goto(`${base}/reports/${reportId}/`);
      const form = hod.locator('form[action$="/approve/"]');
      assert.equal(await form.count(), 1);
      await form.locator('[name="confirmed"]').check();
      await form.locator('button').click();
      await hod.waitForLoadState('networkidle');
      assert((await hod.locator('body').innerText()).includes('APPROVED FOR EXPORT'),
        `Issue failed: ${(await hod.locator('body').innerText()).slice(-500)}`);
      await screenshot(hod, 'hod-mfa-issued');
      const download = hod.waitForEvent('download');
      await hod.locator('a').filter({hasText: 'Download PDF'}).first().click();
      const saved = await download;
      await saved.saveAs(path.join(evidenceDir, 'synthetic-issued.pdf'));
      issued = fs.readFileSync(path.join(evidenceDir, 'synthetic-issued.pdf'));
      assert(issued.includes(Buffer.from('/ByteRange')), 'PDF lacks byte-range signature');
    });
    await stage('delivery_outbox', async () => {
      await hod.goto(`${base}/reports/${reportId}/delivery/`);
      await hod.locator('[name="recipient"]').fill('synthetic-customer@example.test');
      const download = hod.waitForEvent('download');
      await hod.locator('button[name="action"][value="email_draft"]').click();
      await (await download).saveAs(path.join(evidenceDir, 'outbox-NOT-SENT.eml'));
      await screenshot(hod, 'delivery-outbox-not-sent');
    });
    await stage('customer_download', async () => {
      await customer.reload();
      assert((await customer.locator('body').innerText()).includes('Download approved PDF'));
      await screenshot(customer, 'customer-status-issued');
      const download = customer.waitForEvent('download');
      await customer.locator('a').filter({hasText: 'Download approved PDF'}).click();
      await (await download).saveAs(path.join(evidenceDir, 'customer-downloaded.pdf'));
      const copy = fs.readFileSync(path.join(evidenceDir, 'customer-downloaded.pdf'));
      assert.equal(crypto.createHash('sha256').update(copy).digest('hex'),
        crypto.createHash('sha256').update(issued).digest('hex'));
    });
    await stage('qr_and_tamper', async () => {
      await hod.goto(`${base}/reports/${reportId}/`);
      const verify = hod.locator('a').filter({hasText: 'Verify issued PDF'});
      assert.equal(await verify.count(), 1);
      await hod.goto(new URL(await verify.getAttribute('href'), base).href);
      assert((await hod.locator('body').innerText()).includes('VALID'));
      await screenshot(hod, 'qr-valid');
      const tampered = path.join(evidenceDir, 'tampered-for-check.pdf');
      fs.writeFileSync(tampered, Buffer.concat([issued, Buffer.from('x')]));
      await hod.locator('input[type="file"]').setInputFiles(tampered);
      await hod.locator('button').filter({hasText: 'Verify PDF'}).click();
      assert((await hod.locator('body').innerText()).includes('NOT VALID'));
      await screenshot(hod, 'tampered-not-valid');
    });
    await stage('audit_screen', async () => {
      await admin.goto(`${base}/jobs/${jobId}/audit/`);
      await screenshot(admin, 'job-audit-history');
    });
    const result = {status: 'UI_FLOW_PASS', sample: 'SYN-FULL-001', reportId,
      elapsedSeconds: Math.round((Date.now() - started) / 1000), stages,
      delivery: 'EMAIL DRAFT IN OUTBOX — NOT SENT; SMTP absent'};
    fs.writeFileSync(path.join(evidenceDir, 'run.json'), JSON.stringify(result, null, 2));
    process.stdout.write(JSON.stringify(result) + '\n');
  } finally {
    for (const context of contexts) await context.close();
    await browser.close();
  }
})().catch(error => { console.error(error.stack || String(error)); process.exit(1); });
