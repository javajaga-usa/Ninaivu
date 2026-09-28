import {validate} from '../models/adjustments.mjs';
export async function render(bitmap, adjustments, maxSide = Infinity) {
  const a = validate(adjustments);
  const scale = Math.min(1, maxSide / Math.max(bitmap.width, bitmap.height));
  let sw = bitmap.width, sh = bitmap.height;
  const ratio = {square: 1, landscape: 16/9, portrait: 4/5}[a.crop];
  if (ratio) { if (sw/sh > ratio) sw = sh*ratio; else sh = sw/ratio; }
  const w = Math.max(1, Math.round(sw*scale)), h = Math.max(1, Math.round(sh*scale));
  const canvas = typeof OffscreenCanvas !== 'undefined' ? new OffscreenCanvas(w,h) : Object.assign(document.createElement('canvas'), {width:w,height:h});
  const ctx = canvas.getContext('2d', {willReadFrequently: true});
  ctx.save(); ctx.translate(w/2,h/2);
  const angle = a.angle*Math.PI/180;
  // Zoom to avoid empty corners when straightening.
  const zoom = Math.max(Math.cos(angle)+Math.abs(Math.sin(angle))*h/w, Math.cos(angle)+Math.abs(Math.sin(angle))*w/h);
  ctx.rotate(angle); ctx.scale(zoom,zoom);
  ctx.drawImage(bitmap, (bitmap.width-sw)/2, (bitmap.height-sh)/2, sw,sh, -w/2,-h/2,w,h); ctx.restore();
  const pixels = ctx.getImageData(0,0,w,h), d = pixels.data;
  const exposure = 2**(a.exposure/100), contrast = 1+a.contrast/100, saturation = 1+a.saturation/100;
  for (let y=0;y<h;y++) {
    for (let x=0;x<w;x++) {
      const i=(y*w+x)*4;
      let r=d[i]*exposure+a.warmth*.3, g=d[i+1]*exposure, b=d[i+2]*exposure-a.warmth*.3;
      const tone=Math.max(0,Math.min(1,(.2126*r+.7152*g+.0722*b)/255));
      const lift=a.shadows*.9*(1-tone)**3+a.highlights*.9*tone**3;
      r+=lift;g+=lift;b+=lift;
      const l=.2126*r+.7152*g+.0722*b;
      const radius=((x-w/2)/(w/2))**2+((y-h/2)/(h/2))**2;
      const vignette=1-a.vignette/100*.55*Math.min(1,radius/2);
      d[i]=(((l+(r-l)*saturation)-128)*contrast+128)*vignette;
      d[i+1]=(((l+(g-l)*saturation)-128)*contrast+128)*vignette;
      d[i+2]=(((l+(b-l)*saturation)-128)*contrast+128)*vignette;
    }
    if (y%128===0) await new Promise(resolve=>setTimeout(resolve,0));
  }
  if (a.noise || a.sharpness) {
    const source = new Uint8ClampedArray(d);
    for (let y=1;y<h-1;y++) {
      for (let x=1;x<w-1;x++) {
        const i=(y*w+x)*4;
        for(let c=0;c<3;c++) {
          let sum=0;
          for(let dy=-1;dy<=1;dy++) for(let dx=-1;dx<=1;dx++) sum+=source[i+(dy*w+dx)*4+c];
          const average=sum/9, original=source[i+c];
          d[i+c]=original+(average-original)*a.noise/100+(original-average)*a.sharpness/100;
        }
      }
      if(y%64===0) await new Promise(resolve=>setTimeout(resolve,0));
    }
  }
  ctx.putImageData(pixels,0,0);
  return canvas;
}
