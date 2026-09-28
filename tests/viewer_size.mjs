/* Verify the painted photo fills the available viewer area without cropping. */
import { createRequire } from 'node:module';
import fs from 'node:fs/promises';
import path from 'node:path';
import assert from 'node:assert/strict';
const { chromium } = createRequire(import.meta.url)('playwright');
const browser = await launch({channel:'msedge',headless:true});
try {
  const page = await browser.newPage();
  await page.route('http://ninaivu.test/**', async route => {
    const pathname = new URL(route.request().url()).pathname;
    if (pathname === '/') return route.fulfill({contentType:'text/html',body:`
      <link rel="stylesheet" href="/css/style.css"><link rel="stylesheet" href="/css/editor.css">
      <div class="viewer"><div class="viewer-stage"><img></div></div>`});
    return route.fulfill({contentType:pathname.endsWith('.css')?'text/css':'text/javascript',
      body:await fs.readFile(path.join('ninaivu/static',pathname))});
  });
  for (const viewport of [{width:1440,height:900},{width:390,height:844}]) {
    await page.setViewportSize(viewport);await page.goto('http://ninaivu.test/');
    for (const [width,height] of [[400,300],[300,400],[2400,1600],[1600,2400]]) {
      for (const rotate of [0,90,180,270]) {
        const result = await page.evaluate(async ({width,height,rotate}) => {
          const { Viewer } = await import('/js/viewer.js');
          const img = document.querySelector('.viewer-stage img');
          const source = document.createElement('canvas');source.width=width;source.height=height;
          img.src=source.toDataURL();await img.decode();
          const stage = document.querySelector('.viewer-stage'),style=getComputedStyle(stage);
          const availableW=stage.clientWidth-parseFloat(style.paddingLeft)-parseFloat(style.paddingRight);
          const availableH=stage.clientHeight-parseFloat(style.paddingTop)-parseFloat(style.paddingBottom);
          const fit=Viewer.prototype.fitScale.call({media:img,transform:{rotate}});
          const contain=Math.min(img.offsetWidth/width,img.offsetHeight/height);
          const rotated=rotate%180!==0;
          const paintedW=(rotated?height:width)*contain*fit;
          const paintedH=(rotated?width:height)*contain*fit;
          return {availableW,availableH,paintedW,paintedH};
        },{width,height,rotate});
        const {availableW:w,availableH:h,paintedW:pw,paintedH:ph}=result;
        assert.ok(pw<=w+1&&ph<=h+1,JSON.stringify(result));
        assert.ok(Math.abs(pw-w)<1||Math.abs(ph-h)<1,`Photo underfills viewer: ${JSON.stringify(result)}`);
      }
    }
    console.log(`PASS ${viewport.width}×${viewport.height}: small/large portrait/landscape, all quarter turns`);
  }
} finally {await browser.close();}
