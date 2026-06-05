const { chromium, devices } = require('playwright');
const path = require('path');
const fs = require('fs');

async function main() {
  const outDir = path.resolve(__dirname, 'images');
  fs.mkdirSync(outDir, { recursive: true });

  const browser = await chromium.launch();

  // 1) モバイル PWA（録音画面）
  const mobileCtx = await browser.newContext({
    ...devices['iPhone 13'],
    locale: 'ja-JP',
  });
  const mobilePage = await mobileCtx.newPage();
  await mobilePage.goto('https://jizo-dev.com/ai-voice-memo/', { waitUntil: 'networkidle' });
  await mobilePage.waitForTimeout(1500);
  const mobilePath = path.join(outDir, 'mobile_recording.png');
  await mobilePage.screenshot({ path: mobilePath, fullPage: false });
  console.log('mobile saved:', mobilePath);
  await mobileCtx.close();

  // 2) 管理画面（Basic 認証）
  const adminCtx = await browser.newContext({
    viewport: { width: 1440, height: 900 },
    locale: 'ja-JP',
    httpCredentials: { username: 'test', password: 'test' },
  });
  async function login(page){
    // 既に sessionStorage に認証情報があれば login overlay は最初から非表示
    const visible = await page.isVisible('#login-overlay').catch(() => false);
    if (visible) {
      await page.fill('#login-user', 'test');
      await page.fill('#login-pass', 'test');
      await page.click('#login-btn');
    }
    await page.waitForFunction(() => {
      const el = document.getElementById('login-overlay');
      return el && el.classList.contains('hidden');
    }, { timeout: 10000 });
    await page.waitForTimeout(2000);
  }

  // 管理画面 トップ
  const adminPage2 = await adminCtx.newPage();
  await adminPage2.goto('https://jizo-dev.com/ai-voice-memo/admin/', { waitUntil: 'networkidle' });
  await login(adminPage2);
  const adminTopPath = path.join(outDir, 'admin_top.png');
  await adminPage2.screenshot({ path: adminTopPath, fullPage: false });
  console.log('admin top saved:', adminTopPath);

  // 管理画面 プロジェクト詳細
  const adminPage = await adminCtx.newPage();
  await adminPage.goto('https://jizo-dev.com/ai-voice-memo/admin/', { waitUntil: 'networkidle' });
  await login(adminPage);
  const firstProj = await adminPage.$('#project-list .proj-card, #project-list [onclick*="selectProject"], #project-list li');
  if (firstProj) {
    await firstProj.click();
    await adminPage.waitForTimeout(3500);
  }
  const adminPath = path.join(outDir, 'admin_project.png');
  await adminPage.screenshot({ path: adminPath, fullPage: false });
  console.log('admin saved:', adminPath);

  await browser.close();
}

main().catch(err => { console.error(err); process.exit(1); });
