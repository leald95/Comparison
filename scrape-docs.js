const puppeteer = require('puppeteer');

(async () => {
  const browser = await puppeteer.launch({ headless: true });
  const page = await browser.newPage();
  
  // Navigate to the local docs server
  await page.goto('http://localhost:8080/index.html', { waitUntil: 'networkidle0', timeout: 30000 });
  
  // Wait for content to load
  await page.waitForSelector('body', { timeout: 10000 });
  
  // Wait a bit for React to render
  await new Promise(r => setTimeout(r, 3000));
  
  // Get all text content
  const content = await page.evaluate(() => document.body.innerText);
  console.log('=== PAGE CONTENT ===');
  console.log(content);
  
  // Try to find navigation links for scripting section
  const links = await page.evaluate(() => {
    const anchors = Array.from(document.querySelectorAll('a'));
    return anchors.map(a => ({ href: a.href, text: a.innerText })).filter(l => l.text.length > 0);
  });
  
  console.log('\n=== NAVIGATION LINKS ===');
  links.forEach(l => console.log(`${l.text}: ${l.href}`));
  
  await browser.close();
})();
