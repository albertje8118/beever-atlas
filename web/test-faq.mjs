import { chromium } from 'playwright';

const errors = [];
const browser = await chromium.launch({ headless: true });
const ctx = await browser.newContext();
const page = await ctx.newPage();
page.on('pageerror', e => errors.push('PAGEERROR: ' + e.message + '\n' + (e.stack || '').split('\n').slice(0, 6).join('\n')));
page.on('console', m => { if (m.type() === 'error') errors.push('CONSOLE: ' + m.text()); });

// Inject the API key as VITE env via localStorage isn't enough; we just hit the page and let the bundle do its thing
await page.goto('http://localhost:5173/channels/719437274441449572/wiki', { waitUntil: 'networkidle', timeout: 20000 });
await page.waitForTimeout(2500);
console.log('LOADED. URL:', page.url(), 'errs:', errors.length);
errors.forEach(e => console.log(' ', e));
errors.length = 0;

const txt = await page.locator('body').innerText();
console.log('--- visible body text snippet ---');
console.log(txt.slice(0, 600));

// Click FAQ
console.log('\n--- click FAQ ---');
const faq = page.locator('button:has-text("FAQ")');
const cnt = await faq.count();
console.log('FAQ buttons:', cnt);
if (cnt > 0) {
  await faq.first().click();
  await page.waitForTimeout(2500);
}
console.log('After click. errs:', errors.length, 'URL:', page.url());
errors.forEach(e => console.log(' ', e));

const rootHtml = await page.locator('#root').innerHTML();
console.log('root html length:', rootHtml.length);
const txt2 = await page.locator('body').innerText();
console.log('--- visible body text snippet AFTER click ---');
console.log(txt2.slice(0, 600));

await browser.close();
