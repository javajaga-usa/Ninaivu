import {describeServerJob} from '../services/AIPhotoService.mjs';
import * as i18n from '../../i18n.js';
import {errorText} from '../utils/messages.mjs';
import {blendWeights, blendOnto, keepAt} from '../../studio/hairstyle-mask.mjs';
/**
 * Paint an area and have it removed — or, with `ask`, redrawn to a request on the
 * AI server: `ask` is `{title, lede, prompt, done}`, and Sudar's Add hair is one.
 *
 * Sudar's hairstyles add to `ask`:
 *   `people`   `[{id, label, thumb}]`: whose hair, when there is more than one person
 *   `masks(id, w, h)`  `{change, keep}` for that person at `w × h` (one byte a pixel):
 *              `change` is painted in to start with; `keep` cannot be painted, is not
 *              sent, and is put back from the photograph after every drawing
 *   `file`     the download's file name
 */
export function openRemoveObject(source, service, {provider = 'local', onApply = null, ask = null} = {}) {
  const dialog=document.createElement('dialog');
  dialog.className='ap-recolor ap-remove-dialog';
  dialog.setAttribute('aria-label',ask?ask.title:i18n.t('Remove object'));
  dialog.innerHTML=`
    <div class="u-6ab333">
      <div class="u-0c88fe">
        <span class="ap-brand-mark u-25f681"  aria-hidden="true">✦</span>
        <div>
          <h2 class="u-b16a11">${ask?ask.title:i18n.t('Object removal & inpainting')}</h2>
          <span class="u-c7bb46">${ask?ask.lede:i18n.t('Fill selected area from surrounding pixels')}</span>
        </div>
      </div>
      <button class="btn u-e065b4" data-close-remove aria-label="${i18n.t('Close remove object')}" >✕</button>
    </div>
    <div class="ap-recolor-tools">
      <label class="u-287c0a">
        ${i18n.t('Brush size')}
        <input data-size type="range" min="4" max="140" value="30" class="u-127a9d">
      </label>
      <label class="u-8eede2">
        <input data-erase type="checkbox"> ${i18n.t('Erase selection')}
      </label>
      <label class="u-8eede2">
        <input data-mask type="checkbox" checked> ${i18n.t('Show selection')}
      </label>
    </div>
    ${ask&&ask.people&&ask.people.length>1?`<div class="ap-hairstyle-people u-273a67" role="radiogroup" aria-label="${i18n.t('Whose hair')}" ><span class="u-750b3f">${i18n.t('Whose hair')}</span></div>`:''}
    <canvas aria-label="${ask?i18n.t('Paint the area to redraw'):i18n.t('Paint the area to remove')}" tabindex="0"></canvas>
    ${ask?`<label class="u-97f187">${i18n.t('What should be drawn there')}
      <textarea data-ask rows="2" maxlength="2000" class="u-d2d96f">${ask.prompt.replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]))}</textarea></label>`:''}
    <p class="u-c6bc9e">${i18n.t('Drag with mouse or touch. With keyboard, move brush with arrow keys and hold Space to paint.')}</p>
    <div class="u-0fcd2e">
      <button class="btn" data-clear>${i18n.t('Clear selection')}</button>
      <button class="btn primary" data-apply disabled>${ask?i18n.t('Draw it'):i18n.t('Remove selected area')}</button>
      <button class="btn" data-apply-photo disabled ${onApply?'':'hidden'}>${i18n.t('Apply to photo')}</button>
      <button class="btn" data-download disabled>${i18n.t('Download result')}</button>
    </div>
    <p role="status" class="u-8afc9b">${i18n.t('Select the area before applying.')}</p>`;

  document.body.append(dialog);
  const $=s=>dialog.querySelector(s), canvas=$('canvas');
  const scale=Math.min(1,1000/Math.max(source.width,source.height));
  canvas.width=Math.round(source.width*scale);canvas.height=Math.round(source.height*scale);
  const context=canvas.getContext('2d'), mask=document.createElement('canvas');mask.width=canvas.width;mask.height=canvas.height;
  const brush=mask.getContext('2d');context.drawImage(source,0,0,canvas.width,canvas.height);
  let original=context.getImageData(0,0,canvas.width,canvas.height);
  // Pixels that are put back from the photograph after every drawing: a face.
  let keep=null, keepTint=null, keepFull=null;
  function choose(id){
    if(!ask||!ask.masks)return;
    const {change,keep:kept}=ask.masks(id,mask.width,mask.height);
    keep=kept;keepFull=null;
    const paint=new ImageData(mask.width,mask.height),tint=new ImageData(mask.width,mask.height);
    for(let i=0,k=0;i<change.length;i++,k+=4){
      if(change[i]){paint.data[k]=255;paint.data[k+1]=64;paint.data[k+2]=129;paint.data[k+3]=255;}
      if(keep[i]){tint.data[k]=40;tint.data[k+1]=205;tint.data[k+2]=178;tint.data[k+3]=255;}
    }
    brush.clearRect(0,0,mask.width,mask.height);brush.putImageData(paint,0,0);
    keepTint=document.createElement('canvas');keepTint.width=mask.width;keepTint.height=mask.height;keepTint.getContext('2d').putImageData(tint,0,0);
    selected=change.some(v=>v);
    for(const b of dialog.querySelectorAll('[data-person]'))b.setAttribute('aria-checked',String(Number(b.dataset.person)===id));
  }
  /** What is painted, less what is kept: one byte a pixel at the mask's size. */
  function paintedBytes(){
    const rgba=brush.getImageData(0,0,mask.width,mask.height).data,out=new Uint8Array(mask.width*mask.height);
    for(let i=0;i<out.length;i++)out[i]=keep&&keep[i]?0:rgba[i*4+3];
    return out;
  }
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
      if(keepTint){context.globalAlpha=.35;context.drawImage(keepTint,0,0);}
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
    selected=keep?paintedBytes().some(v=>v>0):brush.getImageData(0,0,mask.width,mask.height).data.some((v,i)=>i%4===3&&v>0);
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
      let sent=mask;
      if(keep){
        // The kept face is not sent to be redrawn at all.
        const bytes=paintedBytes(),image=new ImageData(mask.width,mask.height);
        for(let i=0;i<bytes.length;i++){image.data[i*4]=image.data[i*4+1]=image.data[i*4+2]=255;image.data[i*4+3]=bytes[i];}
        sent=document.createElement('canvas');sent.width=mask.width;sent.height=mask.height;sent.getContext('2d').putImageData(image,0,0);
      }
      fullMask.getContext('2d').drawImage(sent,0,0,fullMask.width,fullMask.height);
      const maskBlob=await new Promise((resolve,reject)=>fullMask.toBlob(b=>b?resolve(b):reject(new Error(i18n.t('Export failed.'))),'image/png'));
      if(closed)return;
      const blob=ask
        ?await service.inpaint(current,maskBlob,$('[data-ask]').value,controller.signal,{onStatus:state=>{$('[role=status]').textContent=describeServerJob(state);}})
        :await service.removeObject(current,maskBlob,controller.signal,{serverJob:provider==='ai-server',onStatus:state=>{$('[role=status]').textContent=describeServerJob(state);}});
      if(closed)return;
      let drawnBlob=blob;
      if(keep)drawnBlob=await keptOnto(blob,paintedBytes());
      if(closed)return;
      const resultBitmap=await createImageBitmap(drawnBlob);
      if(closed){resultBitmap.close();return;}
      resultBlob=drawnBlob;
      if(current!==source)current.close();
      current=resultBitmap;
      context.clearRect(0,0,canvas.width,canvas.height);context.drawImage(current,0,0,canvas.width,canvas.height);
      original=context.getImageData(0,0,canvas.width,canvas.height);
      // A hairstyle keeps its area, so drawing again is one press; anything else starts clean.
      if(!keep){brush.clearRect(0,0,mask.width,mask.height);selected=false;}
      $('[data-download]').disabled=false;$('[data-apply-photo]').disabled=false;
      $('[role=status]').textContent=ask?ask.done:onApply?i18n.t('Done. Apply it to the photo, download it, or paint another area and apply again.'):i18n.t('Done. Download the result, or paint another area and apply again.');
    } catch(error) {if(!closed&&error.name!=='AbortError')$('[role=status]').textContent=errorText(error,i18n.t);}
    finally {busy=false;render();}
  };

  /** The drawing laid onto the picture through what was painted, the kept face put back byte for byte. */
  async function keptOnto(blob,painted){
    const W=current.width,H=current.height;
    const read=image=>{const c=document.createElement('canvas');c.width=W;c.height=H;const x=c.getContext('2d',{willReadFrequently:true});x.drawImage(image,0,0,W,H);return x.getImageData(0,0,W,H);};
    const drawn=await createImageBitmap(blob);
    try{
      const base=read(current),made=read(drawn);
      keepFull||=keepAt(keep,mask.width,mask.height,W,H);
      const weights=blendWeights(painted,keep,mask.width,mask.height,3);
      const out=new ImageData(blendOnto(base.data,made.data,W,H,weights,mask.width,mask.height,keepFull),W,H);
      const c=document.createElement('canvas');c.width=W;c.height=H;c.getContext('2d').putImageData(out,0,0);
      return await new Promise((resolve,reject)=>c.toBlob(b=>b?resolve(b):reject(new Error(i18n.t('Export failed.'))),'image/png'));
    }finally{drawn.close();}
  }

  $('[data-download]').onclick=()=>{
    if(!resultBlob)return;
    const url=URL.createObjectURL(resultBlob),a=document.createElement('a');a.href=url;a.download=ask&&ask.file||'ninaivu-object-removed.png';a.click();setTimeout(()=>URL.revokeObjectURL(url),30000);
  };
  $('[data-apply-photo]').onclick=async()=>{
    if(!resultBlob||busy||!onApply)return;
    try{
      const bitmap=await createImageBitmap(resultBlob);
      dialog.close();
      onApply(bitmap);
    }catch(error){$('[role=status]').textContent=errorText(error,i18n.t);}
  };

  if(ask&&ask.masks){
    const row=$('.ap-hairstyle-people');
    for(const person of row?ask.people:[]){
      const b=document.createElement('button');b.type='button';b.className='btn';b.dataset.person=person.id;b.setAttribute('role','radio');
      b.setAttribute('aria-label',person.label);b.title=person.label;b.style.cssText='padding:2px; border-radius:50%; width:40px; height:40px; overflow:hidden;';
      if(person.thumb){person.thumb.style.cssText='width:100%; height:100%; border-radius:50%; display:block;';b.append(person.thumb);}else b.textContent=person.label;
      b.onclick=()=>{if(!busy){choose(person.id);render();}};row.append(b);
    }
    choose(ask.people&&ask.people.length?ask.people[0].id:0);render();
    $('[role=status]').textContent=i18n.t('Check the marked hair, then draw it.');
  }
  $('[data-close-remove]').onclick=()=>dialog.close();
  dialog.onclose=()=>{closed=true;controller?.abort();current.close();if(current!==source)source.close();dialog.remove();};
  dialog.showModal();
}
