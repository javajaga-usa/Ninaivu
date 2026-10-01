import {describeServerJob} from '../services/AIPhotoService.mjs';
import * as i18n from '../../i18n.js';
import {errorText} from '../utils/messages.mjs';
/**
 * Paint an area and have it removed — or, with `ask`, redrawn to a request on the
 * AI server: `ask` is `{title, lede, prompt, done}`, and Sudar's Add hair is one.
 */
export function openRemoveObject(source, service, {provider = 'local', onApply = null, ask = null} = {}) {
  const dialog=document.createElement('dialog');
  dialog.className='ap-recolor ap-remove-dialog';
  dialog.setAttribute('aria-label',ask?ask.title:i18n.t('Remove object'));
  dialog.innerHTML=`
    <div style="display:flex; align-items:center; justify-content:space-between; margin-bottom:12px; padding-bottom:10px; border-bottom:1px solid var(--ap-line);">
      <div style="display:flex; align-items:center; gap:9px;">
        <span class="ap-brand-mark" style="width:28px; height:28px; font-size:15px;" aria-hidden="true">✦</span>
        <div>
          <h2 style="margin:0; font-size:15px; font-weight:650; letter-spacing:-0.2px;">${ask?ask.title:i18n.t('Object removal & inpainting')}</h2>
          <span style="font-size:10.5px; color:var(--ap-muted);">${ask?ask.lede:i18n.t('Fill selected area from surrounding pixels')}</span>
        </div>
      </div>
      <button class="btn" data-close-remove aria-label="${i18n.t('Close remove object')}" style="width:28px; height:28px; padding:0; border-radius:50%; display:grid; place-items:center;">✕</button>
    </div>
    <div class="ap-recolor-tools">
      <label style="font-size:11.5px; font-weight:550; color:var(--ap-muted);">
        ${i18n.t('Brush size')}
        <input data-size type="range" min="4" max="140" value="30" style="width:120px; margin:0 6px; accent-color:var(--ap-accent);">
      </label>
      <label style="font-size:11.5px; cursor:pointer; color:var(--ap-muted); display:flex; align-items:center; gap:5px;">
        <input data-erase type="checkbox"> ${i18n.t('Erase selection')}
      </label>
      <label style="font-size:11.5px; cursor:pointer; color:var(--ap-muted); display:flex; align-items:center; gap:5px;">
        <input data-mask type="checkbox" checked> ${i18n.t('Show selection')}
      </label>
    </div>
    <canvas aria-label="${ask?i18n.t('Paint the area to redraw'):i18n.t('Paint the area to remove')}" tabindex="0"></canvas>
    ${ask?`<label style="display:flex; flex-direction:column; gap:4px; font-size:11.5px; color:var(--ap-muted); margin:8px 0 0;">${i18n.t('What should be drawn there')}
      <textarea data-ask rows="2" maxlength="2000" style="font:inherit; font-size:12px; background:var(--ap-card); color:var(--ap-text); border:1px solid var(--ap-line); border-radius:6px; padding:6px 8px;">${ask.prompt.replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]))}</textarea></label>`:''}
    <p style="font-size:11px; color:var(--ap-dim); margin:8px 0;">${i18n.t('Drag with mouse or touch. With keyboard, move brush with arrow keys and hold Space to paint.')}</p>
    <div style="display:flex; gap:8px; flex-wrap:wrap; margin:12px 0;">
      <button class="btn" data-clear>${i18n.t('Clear selection')}</button>
      <button class="btn primary" data-apply disabled>${ask?i18n.t('Draw it'):i18n.t('Remove selected area')}</button>
      <button class="btn" data-apply-photo disabled ${onApply?'':'hidden'}>${i18n.t('Apply to photo')}</button>
      <button class="btn" data-download disabled>${i18n.t('Download result')}</button>
    </div>
    <p role="status" style="font-size:11.5px; color:var(--ap-muted); margin:0;">${i18n.t('Select the area before applying.')}</p>`;

  document.body.append(dialog);
  const $=s=>dialog.querySelector(s), canvas=$('canvas');
  const scale=Math.min(1,1000/Math.max(source.width,source.height));
  canvas.width=Math.round(source.width*scale);canvas.height=Math.round(source.height*scale);
  const context=canvas.getContext('2d'), mask=document.createElement('canvas');mask.width=canvas.width;mask.height=canvas.height;
  const brush=mask.getContext('2d');context.drawImage(source,0,0,canvas.width,canvas.height);
  let original=context.getImageData(0,0,canvas.width,canvas.height);
  let current=source, selected=false, painting=false, last=null, keyboard={x:canvas.width/2,y:canvas.height/2}, held=false, busy=false, closed=false, resultBlob=null, controller=null;

  function render(){
    if(closed)return;
    context.putImageData(original,0,0);
    if($('[data-mask]').checked){
      context.save();
      context.globalAlpha=.4;
      context.fillStyle='#ff4081';
      context.globalCompositeOperation='source-over';
      context.drawImage(mask,0,0);
      context.restore();
    }
    $('[data-apply]').disabled=!selected||busy;
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

  canvas.onpointerdown=e=>{if(busy)return;e.preventDefault();painting=true;last=null;canvas.setPointerCapture(e.pointerId);stroke(position(e));};
  canvas.onpointermove=e=>{if(painting)stroke(position(e));};
  canvas.onpointerup=canvas.onpointercancel=()=>{painting=false;last=null;};

  canvas.onkeydown=e=>{
    if(busy||!['ArrowLeft','ArrowRight','ArrowUp','ArrowDown',' '].includes(e.key))return;
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

  $('[data-apply]').onclick=async()=>{
    if(busy||!selected)return;
    busy=true;render();$('[role=status]').textContent=ask?i18n.t('Drawing in the painted area… This runs on the AI server on your home network.'):provider==='ai-server'?i18n.t('Removing the selected area… This runs on the AI server on your home network.'):provider==='local-ai'?i18n.t('Removing the selected area with the LaMa model… This runs on your Ninaivu server.'):i18n.t('Removing the selected area… This runs on your Ninaivu server.');
    controller=new AbortController();
    try {
      const fullMask=document.createElement('canvas');fullMask.width=current.width;fullMask.height=current.height;
      fullMask.getContext('2d').drawImage(mask,0,0,fullMask.width,fullMask.height);
      const maskBlob=await new Promise((resolve,reject)=>fullMask.toBlob(b=>b?resolve(b):reject(new Error(i18n.t('Export failed.'))),'image/png'));
      if(closed)return;
      const blob=ask
        ?await service.inpaint(current,maskBlob,$('[data-ask]').value,controller.signal,{onStatus:state=>{$('[role=status]').textContent=describeServerJob(state);}})
        :await service.removeObject(current,maskBlob,controller.signal,{serverJob:provider==='ai-server',onStatus:state=>{$('[role=status]').textContent=describeServerJob(state);}});
      if(closed)return;
      const resultBitmap=await createImageBitmap(blob);
      if(closed){resultBitmap.close();return;}
      resultBlob=blob;
      if(current!==source)current.close();
      current=resultBitmap;
      context.clearRect(0,0,canvas.width,canvas.height);context.drawImage(current,0,0,canvas.width,canvas.height);
      original=context.getImageData(0,0,canvas.width,canvas.height);
      brush.clearRect(0,0,mask.width,mask.height);selected=false;
      $('[data-download]').disabled=false;$('[data-apply-photo]').disabled=false;
      $('[role=status]').textContent=ask?ask.done:onApply?i18n.t('Done. Apply it to the photo, download it, or paint another area and apply again.'):i18n.t('Done. Download the result, or paint another area and apply again.');
    } catch(error) {if(!closed&&error.name!=='AbortError')$('[role=status]').textContent=errorText(error,i18n.t);}
    finally {busy=false;render();}
  };

  $('[data-download]').onclick=()=>{
    if(!resultBlob)return;
    const url=URL.createObjectURL(resultBlob),a=document.createElement('a');a.href=url;a.download='ninaivu-object-removed.png';a.click();setTimeout(()=>URL.revokeObjectURL(url),30000);
  };
  $('[data-apply-photo]').onclick=async()=>{
    if(!resultBlob||busy||!onApply)return;
    try{
      const bitmap=await createImageBitmap(resultBlob);
      dialog.close();
      onApply(bitmap);
    }catch(error){$('[role=status]').textContent=errorText(error,i18n.t);}
  };

  $('[data-close-remove]').onclick=()=>dialog.close();
  dialog.onclose=()=>{closed=true;controller?.abort();current.close();if(current!==source)source.close();dialog.remove();};
  dialog.showModal();
}
