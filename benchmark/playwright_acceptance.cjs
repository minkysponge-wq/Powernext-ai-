/* Browser acceptance of the customer -> lab -> assigned station path.
   Uses an isolated Django LiveServer test database; no cloud calls. */
const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');

(async () => {
  const base = process.env.VECTORLAB_TEST_URL;
  const password = process.env.VECTORLAB_TEST_PASSWORD;
  const users = JSON.parse(process.env.VECTORLAB_TEST_USERS);
  const browser = await chromium.launch({headless:true,
    executablePath: process.env.VECTORLAB_BROWSER || 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe'});
  const contexts = [];
  const screenshotDir = process.env.VECTORLAB_SCREENSHOT_DIR;
  let screenshotNumber = 0;
  async function evidence(page, label) {
    if (!screenshotDir) return;
    fs.mkdirSync(screenshotDir, {recursive:true});
    const name = `${String(++screenshotNumber).padStart(2,'0')}-${label}.png`;
    await page.screenshot({path:path.join(screenshotDir,name),fullPage:true});
  }
  function token(secretHex) {
    const counter = Buffer.alloc(8);
    counter.writeBigUInt64BE(BigInt(Math.floor(Date.now()/30000)));
    const digest=crypto.createHmac('sha1',Buffer.from(secretHex,'hex')).update(counter).digest();
    const offset=digest.at(-1)&15;
    return String((digest.readUInt32BE(offset)&0x7fffffff)%1000000).padStart(6,'0');
  }
  async function login(role) {
    const username=users[role];
    const context = await browser.newContext();contexts.push(context);
    const page = await context.newPage();
    page.on('dialog', dialog => dialog.accept());
    await page.goto(base + '/accounts/login/');
    await page.locator('input[name="username"]').fill(username);
    await page.locator('input[name="password"]').fill(password);
    await page.locator('button').filter({hasText:'Sign in'}).click();
    await page.waitForLoadState('networkidle');
    let tour = page.locator('#role-tour[open]');
    if (await tour.count()) await tour.locator('#tour-close').click();
    if (page.url().includes('/accounts/otp/')) {
      await page.locator('input[name="token"]').fill(token(users[role+'_otp_secret']));
      await page.locator('button').filter({hasText:'Verify'}).click();
      await page.waitForLoadState('networkidle');
      assert(!page.url().includes('/accounts/otp/'),`OTP failed for ${role}`);
    }
    assert(!page.url().includes('/accounts/login/'), `Login failed for ${username}`);
    tour = page.locator('#role-tour[open]');
    if (await tour.count()) await tour.locator('#tour-close').click();
    return page;
  }
  try {
    const customer = await login('customer');
    await customer.goto(base + '/customer/requests/new/');
    await evidence(customer,'customer-request-form');
    await customer.locator('[name="customer"]').fill('Exact Customer, Ltd.');
    await customer.locator('[name="customer_address"]').fill('12 Test Road, Bengaluru');
    await customer.locator('[name="manufacturer"]').fill('Maker & Co.');
    await customer.locator('#wizard-next').click();
    await customer.locator('[name="sample_particulars"]').fill('Synthetic routine-test sample');
    await customer.locator('#wizard-next').click();
    await customer.locator('[name="requested_tests"][value="routine_test"]').check();
    await customer.locator('#wizard-next').click();
    await customer.locator('#wizard-submit').click();
    await customer.waitForURL(/\/customer\/requests\/OV-/);
    const fileNumber = decodeURIComponent(customer.url().split('/').filter(Boolean).at(-1));
    assert(fileNumber.startsWith('OV-'));
    assert((await customer.locator('body').innerText()).includes('0 of 1 tests complete'));
    await evidence(customer,'customer-request-submitted');

    const admin = await login('admin');
    await admin.goto(base + '/?q=' + encodeURIComponent(fileNumber));
    const href = await admin.locator('a.job-title').first().getAttribute('href');
    assert(href && /\/jobs\/[0-9a-f-]+\//.test(href), 'Admin cannot find submitted job');
    const jobId = href.match(/\/jobs\/([0-9a-f-]+)\//)[1];
    await admin.goto(base + `/jobs/${jobId}/scope/`);
    await evidence(admin,'lab-scope');
    await admin.locator('[name="sample_code"]').fill('DEMO-SAMPLE');
    await admin.locator('[name="test_series"]').fill('DEMO-SERIES');
    await admin.locator('[name="scope_note"]').fill('Synthetic requested routine test only; remaining sections excluded.');
    await admin.locator('[name="report_scope"][value="routine_test"]').check();
    await admin.locator('button').filter({hasText:'Save report scope'}).click();
    await admin.goto(base + `/jobs/${jobId}/assign-test/`);
    await admin.locator('[name="test_type"]').selectOption('routine_test');
    await admin.locator('[name="station"]').fill('Routine-test bay');
    await admin.locator('[name="assigned_to"]').selectOption(String(users.engineer_id));
    await admin.locator('button').filter({hasText:'Assign test'}).click();
    await evidence(admin,'station-assigned');

    const other = await login('other_engineer');
    await other.goto(base + '/');
    assert(!(await other.locator('body').innerText()).includes(fileNumber), 'Unassigned engineer sees the job');
    const forbidden = await other.goto(base + `/jobs/${jobId}/`);
    assert.equal(forbidden.status(),404,'Unassigned engineer can open the job');

    const engineer = await login('engineer');
    await engineer.goto(base + '/');
    const card = engineer.locator('.task-card').filter({hasText:fileNumber});
    assert.equal(await card.count(),1,'Assigned engineer does not see their test');
    await card.locator('button').filter({hasText:'Start & enter readings'}).click();
    await engineer.waitForURL(/\/digital-review\//);
    const stationBody = await engineer.locator('body').innerText();
    assert(stationBody.includes('Station readings'),stationBody.slice(0,800));
    await evidence(engineer,'station-entry');

    let liveCheck = false;
    for (let pageNumber=1;pageNumber<=4;pageNumber++) {
      await engineer.goto(base + `/jobs/${jobId}/digital-review/?test=routine_test&page=${pageNumber}`);
      const rows = engineer.locator('tr[data-reading-key]');
      const count = await rows.count();
      if (!count) break;
      for (let index=0;index<count;index++) {
        const row = rows.nth(index);
        const key = await row.getAttribute('data-reading-key');
        const value = row.locator('.entry-value textarea,.entry-value input');
        const status = row.locator('.entry-status select');
        const essential = {'series':'DEMO-SERIES','sample_code':'DEMO-SAMPLE',
          'customer':'Exact Customer, Ltd.','induced_observation_BT':'Withstood'};
        if (key === 'induced_duration_BT') {
          await value.fill('60');
          await row.locator('.entry-unit input').fill('minutes');
          liveCheck = (await row.locator('.live-check').innerText()).includes('Unit needs checking');
          await value.fill('');
        }
        if (Object.hasOwn(essential,key)) {
          await value.fill(essential[key]);
          await status.selectOption('verified');
        } else {
          await status.selectOption('not_applicable');
        }
      }
      await engineer.locator('button[name="action"][value="save"]').click();
      await engineer.waitForLoadState('networkidle');
    }
    assert(liveCheck,'Live unit check did not fire');
    await engineer.goto(base + `/jobs/${jobId}/digital-review/?test=routine_test&page=1`);
    const lock = engineer.locator('form[action$="/lock/"]');
    assert.equal(await lock.count(),1,'Station lock remains blocked after review');
    await lock.locator('input[name="confirmed"]').check();
    await lock.locator('button').click();
    await engineer.waitForLoadState('networkidle');
    await evidence(engineer,'station-locked');
    await customer.reload();
    const customerBody = await customer.locator('body').innerText();
    assert(customerBody.includes('1 of 1 tests complete'),customerBody.slice(0,1000));

    await engineer.goto(base + `/jobs/${jobId}/?step=report`);
    const firstReportHref=await engineer.locator('a.revision-link').first().getAttribute('href');
    assert(firstReportHref,'Last station lock did not create a draft');
    await engineer.goto(base+firstReportHref);
    await evidence(engineer,'draft-report');
    const engineerLock=engineer.locator('form[action$="/engineer-lock/"]');
    assert.equal(await engineerLock.count(),1,'Draft has unexpected readiness blockers');
    await engineerLock.locator('input[name="confirmed"]').check();
    await engineerLock.locator('button').click();
    await engineer.waitForLoadState('networkidle');
    await evidence(engineer,'engineer-locked');

    const quality=await login('quality');
    await quality.goto(base+'/review/queue/');
    const reviewHref=await quality.locator('a[href*="/review/reports/"]').first().getAttribute('href');
    assert(reviewHref,'Quality queue did not receive engineer-locked report');
    await quality.goto(base+reviewHref);
    await evidence(quality,'quality-review');
    await quality.locator('details').filter({hasText:'Send back for correction'}).locator('summary').click();
    const returned=quality.locator('form[action$="/return/"]');
    await returned.locator('[name="reason_code"]').selectOption('format');
    await returned.locator('[name="reason_note"]').fill('Confirm revised report scope wording');
    await returned.locator('button').click();
    await quality.waitForLoadState('networkidle');
    await evidence(quality,'quality-return');

    await admin.goto(base+`/jobs/${jobId}/scope/`);
    await admin.locator('[name="scope_note"]').fill('Synthetic routine test only; Quality-requested wording corrected.');
    await admin.locator('button').filter({hasText:'Save report scope'}).click();
    await admin.goto(base+`/jobs/${jobId}/?step=report`);
    await admin.locator('button').filter({hasText:'Create report draft'}).click();
    await admin.waitForLoadState('networkidle');

    await engineer.goto(base+`/jobs/${jobId}/?step=report`);
    const secondReportHref=await engineer.locator('a.revision-link').first().getAttribute('href');
    assert.notEqual(secondReportHref,firstReportHref,'Send-back did not create revision 2');
    const reportId=secondReportHref.match(/\/reports\/([0-9a-f-]+)\//)[1];
    await engineer.goto(base+secondReportHref);
    const secondLock=engineer.locator('form[action$="/engineer-lock/"]');
    await secondLock.locator('input[name="confirmed"]').check();
    await secondLock.locator('button').click();

    await quality.goto(base+`/review/reports/${reportId}/`);
    const qualityApprove=quality.locator('form[action$="/quality-verify/"]');
    await qualityApprove.locator('input[name="confirmed"]').check();
    await qualityApprove.locator('button').click();
    await evidence(quality,'quality-verified');

    const hod=await login('hod');
    await hod.goto(base+`/review/reports/${reportId}/`);
    const sign=hod.locator('form[action$="/approve/"]');
    assert.equal(await sign.count(),1,'HoD sign action is not available after Quality approval');
    await sign.locator('input[name="confirmed"]').check();
    await sign.locator('button').click();
    await hod.waitForLoadState('networkidle');
    await evidence(hod,'hod-issued');
    assert((await hod.locator('body').innerText()).includes('APPROVED FOR EXPORT'));
    await hod.goto(base+`/reports/${reportId}/delivery/`);
    await hod.locator('[name="recipient"]').fill('browser-customer@example.test');
    await hod.locator('[name="confirmed"]').check();
    await hod.locator('button[name="action"][value="send_email"]').click();
    await hod.waitForLoadState('networkidle');
    await evidence(hod,'delivery');
    assert((await hod.locator('body').innerText()).includes('Sent by staff'));
    await customer.reload();
    assert((await customer.locator('body').innerText()).includes('Download approved PDF'));
    await evidence(customer,'customer-issued-status');
    console.log(JSON.stringify({browser_pass:true,file_number:fileNumber,job_id:jobId,
      gates:{request:true,assignment_isolation:true,station_live_check:true,
             station_lock:true,auto_draft_and_customer_notice:true,quality_sendback:true,
             revision_2:true,quality_approve:true,hod_mfa_and_sign:true,
             synthetic_email_and_portal:true}}));
  } finally {
    for (const context of contexts) await context.close();
    await browser.close();
  }
})().catch(error => {console.error(error.stack || String(error));process.exit(1)});
