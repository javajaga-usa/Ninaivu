import {recolorPixels} from '../services/recolor.mjs';
import * as i18n from '../../i18n.js';

export function openRecolor(source) {
  const dialog=document.createElement('dialog');
  dialog.className='ap-recolor ap-recolor-dialog';
  dialog.setAttribute('aria-label',i18n.t('Clothing color editor'));
  dialog.innerHTML=`
    <div style="display:flex; align-items:center; justify-content:space-between; margin-bottom:12px; padding-bottom:10px; border-bottom:1px solid var(--ap-line);">
      <div style="display:flex; align-items:center; gap:9px;">
        <span class="ap-brand-mark" style="width:28px; height:28px; font-size:15px;" aria-hidden="true">✦</span>
        <div>
          <h2 style="margin:0; font-size:15px; font-weight:650; letter-spacing:-0.2px;">${i18n.t('Clothing color studio')}</h2>
          <span style="font-size:10.5px; color:var(--ap-muted);">${i18n.t('Brush clothing to recolor with texture preservation')}</span>
        </div>
      </div>
      <button class="btn" data-close-recolor aria-label="${i18n.t('Close recolor editor')}" style="width:28px; height:28px; padding:0; border-radius:50%; display:grid; place-items:center;">✕</button>
    </div>
    <div class="ap-recolor-tools">
      <label style="font-size:11.5px; font-weight:550; color:var(--ap-muted); display:flex; align-items:center; gap:6px;">
        ${i18n.t('New color')}
        <input type="color" value="#2878d0" style="width:24px; height:24px; padding:0; border:none; border-radius:50%; cursor:pointer;">
      </label>
      <label style="font-size:11.5px; font-weight:550; color:var(--ap-muted); display:flex; align-items:center; gap:6px;">
        ${i18n.t('Brush size')}
        <input data-size type="range" min="2" max="100" value="24" style="width:90px; margin:0 4px; accent-color:var(--ap-accent);">
      </label>
      <label style="font-size:11.5px; font-weight:550; color:var(--ap-muted); display:flex; align-items:center; gap:6px;">
        ${i18n.t('Strength')}
        <input data-strength type="range" min="0" max="100" value="85" style="width:90px; margin:0 4px; accent-color:var(--ap-accent);">
      </label>
      <label style="font-size:11.5px; cursor:pointer; color:var(--ap-muted); display:flex; align-items:center; gap:5px;">
        <input data-erase type="checkbox"> ${i18n.t('Erase')}
      </label>
      <label style="font-size:11.5px; cursor:pointer; color:var(--ap-muted); display:flex; align-items:center; gap:5px;">
        <input data-mask type="checkbox" checked> ${i18n.t('Show mask')}
      </label>
    </div>
    <canvas aria-label="${i18n.t('Paint clothing selection')}" tabindex="0"></canvas>
    <p style="font-size:11px; color:var(--ap-dim); margin:8px 0;">${i18n.t('Drag with mouse or touch. With keyboard, move brush with arrow keys and hold Space to paint.')}</p>
    <div style="display:flex; gap:8px; flex-wrap:wrap; margin:12px 0;">
      <button class="btn" data-clear>${i18n.t('Clear selection')}</button>
      <button class="btn primary" data-download disabled>${i18n.t('Download recolored PNG')}</button>
    </div>
    <p role="status" style="font-size:11.5px; color:var(--ap-muted); margin:0;">${i18n.t('Select clothing before downloading.')}</p>`;

  document.body.append(dialog);
  const $=s=>dialog.querySelector(s), canvas=$('canvas');
  const scale=Math.min(1,1000/Math.max(source.width,source.height));
  canvas.width=Math.round(source.width*scale);canvas.height=Math.round(source.height*scale);
  const context=canvas.getContext('2d'), mask=document.createElement('canvas');mask.width=canvas.width;mask.height=canvas.height;
  const brush=mask.getContext('2d');context.drawImage(source,0,0,canvas.width,canvas.height);
  const original=context.getImageData(0,0,canvas.width,canvas.height);
  let selected=false, painting=false, last=null, keyboard={x:canvas.width/2,y:canvas.height/2}, held=false;
  const color=()=>$('input[type=color]').value.match(/[a-f0-9]{2}/gi).map(v=>parseInt(v,16));

  function render(){
    const pixels=recolorPixels(original.data,brush.getImageData(0,0,mask.width,mask.height).data,color(),Number($('[data-strength]').value)/100);
    context.putImageData(new ImageData(pixels,canvas.width,canvas.height),0,0);
    if($('[data-mask]').checked){
      context.save();
      context.globalAlpha=.35;
      context.drawImage(mask,0,0);
      context.restore();
    }
    $('[data-download]').disabled=!selected;
  }

  function stroke(point){
    brush.globalCompositeOperation=$('[data-erase]').checked?'destination-out':'source-over';
    brush.strokeStyle=brush.fillStyle='#ff4081';
    brush.lineWidth=Number($('[data-size]').value);
    brush.lineCap='round';
    brush.beginPath();
    brush.moveTo((last||point).x,(last||point).y);
    brush.lineTo(point.x,point.y);
    brush.stroke();
    brush.beginPath();
    brush.arc(point.x,point.y,brush.lineWidth/2,0,Math.PI*2);
    brush.fill();
    last=point;
    selected=brush.getImageData(0,0,mask.width,mask.height).data.some((v,i)=>i%4===3&&v>0);
    render();
  }

  function position(e){
    const r=canvas.getBoundingClientRect();
    return{x:(e.clientX-r.left)*canvas.width/r.width,y:(e.clientY-r.top)*canvas.height/r.height};
  }

  canvas.onpointerdown=e=>{e.preventDefault();painting=true;last=null;canvas.setPointerCapture(e.pointerId);stroke(position(e));};
  canvas.onpointermove=e=>{if(painting)stroke(position(e));};
  canvas.onpointerup=canvas.onpointercancel=()=>{painting=false;last=null;};

  canvas.onkeydown=e=>{
    if(!['ArrowLeft','ArrowRight','ArrowUp','ArrowDown',' '].includes(e.key))return;
    e.preventDefault();
    if(e.key===' ')held=true;
    keyboard.x=Math.max(0,Math.min(canvas.width,keyboard.x+(e.key==='ArrowRight'?5:e.key==='ArrowLeft'?-5:0)));
    keyboard.y=Math.max(0,Math.min(canvas.height,keyboard.y+(e.key==='ArrowDown'?5:e.key==='ArrowUp'?-5:0)));
    if(held)stroke(keyboard);
    else{
      render();
      context.strokeStyle='#fff';
      context.strokeRect(keyboard.x-3,keyboard.y-3,6,6);
    }
  };
  canvas.onkeyup=e=>{if(e.key===' '){held=false;last=null;}};
  canvas.onblur=()=>{held=false;last=null;};

  for(const input of dialog.querySelectorAll('input'))input.oninput=render;
  $('[data-clear]').onclick=()=>{brush.clearRect(0,0,mask.width,mask.height);selected=false;render();};

  $('[data-download]').onclick=()=>{
    const output=document.createElement('canvas');output.width=source.width;output.height=source.height;const ctx=output.getContext('2d');ctx.drawImage(source,0,0);const data=ctx.getImageData(0,0,output.width,output.height);
    const fullMask=document.createElement('canvas');fullMask.width=output.width;fullMask.height=output.height;const m=fullMask.getContext('2d');m.drawImage(mask,0,0,output.width,output.height);
    data.data.set(recolorPixels(data.data,m.getImageData(0,0,output.width,output.height).data,color(),Number($('[data-strength]').value)/100));ctx.putImageData(data,0,0);
    output.toBlob(blob=>{
      if(!blob)return;
      const url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download='ninaivu-clothing-color.png';a.click();setTimeout(()=>URL.revokeObjectURL(url),30000);
      $('[role=status]').textContent=i18n.t('Downloaded at full source resolution. Your original is unchanged.');
    },'image/png');
  };

  $('[data-close-recolor]').onclick=()=>dialog.close();
  dialog.onclose=()=>{source.close();dialog.remove();};
  dialog.showModal();
}
