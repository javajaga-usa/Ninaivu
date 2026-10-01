import {decode} from './ai-playground/utils/files.mjs';
import {saveLibraryCopy} from './ai-playground/services/library.mjs';
import {thumbUrl} from './api.js';
import * as i18n from './i18n.js';

//: The creations that are one picture, and so can be saved into the library like an edit.
const PICTURES=['postcard','collage','selective','versions','recipe','calendar'];
//: The creations whose picture can be made in another shape: a square for a profile, a tall one for a story.
const SHAPED=['postcard','collage','selective','versions'];
const FORMATS={landscape:[1440,1080],square:[1080,1080],story:[1080,1920]};

const modes = {
  restoration: [i18n.key('Restoration story'), i18n.key('Compare your original with the edited photo. Export a self-contained interactive story.')],
  album: [i18n.key('Family story album'), i18n.key('Add photos, dates and captions. Attach a recorded memory to your album.')],
  postcard: [i18n.key('Photo postcard'), i18n.key('Create a photo card with a title and personal message.')],
  collage: [i18n.key('Collage studio'), i18n.key('Arrange up to 12 photos; change columns, spacing and background.')],
  compare: [i18n.key('Then & now'), i18n.key('Add a second photo, then align it with zoom and horizontal/vertical position.')],
  slideshow: [i18n.key('Cinematic slideshow'), i18n.key('Play your photos with gentle motion, captions and optional music. Export an offline HTML presentation.')],
  selective: [i18n.key('Selective color'), i18n.key('Paint on the photo to reveal color against a monochrome background.')],
  versions: [i18n.key('Creative versions'), i18n.key('Keep named variations of your current photo. Export a comparison sheet or any individual version.')],
  recipe: [i18n.key('Recipe memories'), i18n.key('Pair a dish photo with ingredients, directions and the story behind it.')],
  calendar: [i18n.key('Photo calendar'), i18n.key('Create a printable month with a favorite photo and dated family occasions.')],
};
const escapeHTML = text => String(text).replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
const readData = blob => new Promise((resolve,reject) => {const reader=new FileReader();reader.onload=()=>resolve(reader.result);reader.onerror=()=>reject(new Error(i18n.t('Could not read file.')));reader.readAsDataURL(blob);});
const download = (blob,name) => {const url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download=name;a.click();setTimeout(()=>URL.revokeObjectURL(url),30000);};

export async function openCreativeStudio({original, edited, returnFocus=document.activeElement, sourceId=null, canSave=false, onSaved=null}) {
  if(document.getElementById('creative-studio'))return;
  if(!document.getElementById('creative-style')){const link=document.createElement('link');link.id='creative-style';link.rel='stylesheet';link.href='/static/css/creative-studio.css';document.head.append(link);}
  const dialog=document.createElement('dialog');dialog.id='creative-studio';dialog.setAttribute('aria-labelledby','cs-title');
  // The dialog is modal, so the language cannot change while it is open: its
  // words are translated once, here, as it is built. `a()` is for a
  // translation that lands inside an attribute.
  const a=escapeHTML;
  dialog.innerHTML=`<header><h1 id="cs-title">${i18n.t('Creative Studio')}</h1><button data-close>${i18n.t('Close')}</button></header>
    <div class="cs-body"><aside><label>${i18n.t('Create')}<select data-mode>${Object.entries(modes).map(([id,[name]])=>`<option value="${id}">${i18n.t(name)}</option>`).join('')}</select></label>
    <p data-help></p><label>${i18n.t('Title')}<input data-title maxlength="90" value="${a(i18n.t('Our family memories'))}"></label>
    <label>${i18n.t('Message / story')}<textarea data-story rows="3" maxlength="1800" placeholder="${a(i18n.t('A memory worth keeping…'))}"></textarea></label>
    <label>${i18n.t('Add photos (JPEG, PNG, WebP)')}<input data-files type="file" accept="image/jpeg,image/png,image/webp" multiple></label>
    <div class="cs-row"><button data-library>${i18n.t('Add from library')}</button></div>
    <div data-picker hidden><div class="cs-picker" data-picker-grid></div><button data-picker-more>${i18n.t('More from the library')}</button></div>
    <label>${i18n.t('Active photo')}<select data-photo></select></label><div class="cs-row"><button data-earlier>${i18n.t('Move earlier')}</button><button data-remove>${i18n.t('Remove photo')}</button></div>
    <label>${i18n.t('Photo caption')}<input data-caption maxlength="180"></label><label>${i18n.t('Photo date')}<input data-date type="date"></label>
    <label data-group="compare">${i18n.t('Compare with')}<select data-second></select></label>
    <label data-group="postcard collage selective versions">${i18n.t('Format')}<select data-format><option value="landscape">${i18n.t('Landscape · 4:3')}</option><option value="square">${i18n.t('Square · 1:1')}</option><option value="story">${i18n.t('Story · 9:16')}</option></select></label>
    <label data-group="collage">${i18n.t('Layout')}<select data-layout><option value="grid">${i18n.t('Grid')}</option><option value="feature">${i18n.t('One large, the rest beside it')}</option><option value="polaroid">${i18n.t('Polaroid frames')}</option></select></label>
    <div data-group="collage versions"><label>${i18n.t('Columns')}<select data-columns><option>2</option><option>3</option><option>4</option></select></label><label>${i18n.t('Spacing')}<input data-gap type="range" min="0" max="40" value="16"></label></div>
    <label data-group="postcard collage recipe calendar versions">${i18n.t('Background')}<input data-paper type="color" value="#fff8eb"></label>
    <div data-group="restoration compare"><label>${i18n.t('Comparison')}<input data-compare type="range" min="0" max="100" value="50"></label><label>${i18n.t('Second photo zoom')}<input data-zoom type="range" min="1" max="3" step="0.05" value="1"></label><label>${i18n.t('Horizontal position')}<input data-x type="range" min="-100" max="100" value="0"></label><label>${i18n.t('Vertical position')}<input data-y type="range" min="-100" max="100" value="0"></label></div>
    <div data-group="album slideshow"><label>${i18n.t('Music or recorded memory (up to 15 MB)')}<input data-audio type="file" accept="audio/*"></label><button data-clear-audio>${i18n.t('Remove audio')}</button><label>${i18n.t('Seconds per slide')}<input data-seconds type="number" min="2" max="30" value="5"></label><button data-play>${i18n.t('Play preview')}</button><button data-stop>${i18n.t('Stop preview')}</button><audio controls hidden></audio></div>
    <div data-group="selective"><label>${i18n.t('Brush size')}<input data-brush type="range" min="8" max="180" value="60"></label><label><input data-erase type="checkbox">${i18n.t('Erase color')}</label><div class="cs-row"><button data-undo-paint>${i18n.t('Undo brush stroke')}</button><button data-clear-paint>${i18n.t('Reset mask')}</button></div><p>${i18n.t('Use a mouse or touch. Keyboard: focus the canvas, move the brush with arrow keys, then press Space to paint.')}</p></div>
    <div data-group="versions"><label>${i18n.t('Look')}<select data-look><option value="none">${i18n.t('Natural')}</option><option value="sepia(.45) saturate(1.25)">${i18n.t('Warm memory')}</option><option value="grayscale(1) contrast(1.15)">${i18n.t('Monochrome')}</option><option value="saturate(.7) contrast(1.2)">${i18n.t('Cinema')}</option></select></label><label>${i18n.t('Version name')}<input data-version-name maxlength="60" value="${a(i18n.t('My look'))}"></label><button data-capture>${i18n.t('Keep version')}</button><button data-version-download>${i18n.t('Download this look')}</button></div>
    <div data-group="recipe"><label>${i18n.t('Ingredients')}<textarea data-ingredients rows="4" maxlength="1600"></textarea></label><label>${i18n.t('Directions')}<textarea data-directions rows="4" maxlength="2200"></textarea></label></div>
    <div data-group="calendar"><label>${i18n.t('Month')}<input data-month type="month"></label><label>${i18n.t('Occasions (one per line: day — event)')}<textarea data-events rows="4" maxlength="900" placeholder="${a(i18n.t('12 — Grandma’s birthday'))}"></textarea></label></div>
    <button class="cs-primary" data-export>${i18n.t('Export creation')}</button><button data-save-library hidden>${i18n.t('Save to library')}</button><button data-print>${i18n.t('Print / save PDF')}</button><p class="cs-note">${i18n.t('Works in your browser. Exports contain the photos and audio you include. Originals are unchanged. Download before closing.')}</p></aside>
    <main><canvas width="1440" height="1080" tabindex="0" aria-label="${a(i18n.t('Creation preview; selective color brush supports arrow keys and Space'))}"></canvas><div data-pages></div></main></div><p role="status" data-status>${i18n.t('Preparing your photos…')}</p>`;
  document.body.append(dialog);
  const $=selector=>dialog.querySelector(selector),canvas=$('canvas'),ctx=canvas.getContext('2d');
  let photos=[],versions=[],closed=false,busy=false,changed=false,audio=null,audioURL=null,audioRevision=0,timer=null,slide=0,maskStrokes=[],stroke=null,brushPoint={x:720,y:540};
  const painted=new WeakMap();
  let capturing=false;
  let comparisonPhoto=null;
  const mask=document.createElement('canvas');mask.width=1440;mask.height=1080;
  const mode=()=>$('[data-mode]').value;
  const active=()=>photos[Number($('[data-photo]').value)||0];
  const status=text=>{$('[data-status]').textContent=text;};
  function stop(){clearInterval(timer);timer=null;$('audio').pause();}
  function cover(context,image,x,y,w,h,filter='none',align=false){
    const scale=Math.max(w/image.width,h/image.height)*(align?Number($('[data-zoom]').value):1);
    const dw=image.width*scale,dh=image.height*scale;
    const dx=align?Number($('[data-x]').value)*(dw-w)/200:0,dy=align?Number($('[data-y]').value)*(dh-h)/200:0;
    context.save();context.beginPath();context.rect(x,y,w,h);context.clip();context.filter=filter;context.drawImage(image,x+(w-dw)/2+dx,y+(h-dh)/2+dy,dw,dh);context.restore();
  }
  function text(value,x,y,width,size=32,maxLines=6,colour='#20232b'){
    ctx.font=`${size}px system-ui`;ctx.fillStyle=colour;ctx.textBaseline='top';
    let lines=0,truncated=false;
    for(const paragraph of String(value).split('\n')){
      let line='';
      for(const word of paragraph.split(/\s+/)){
        if(ctx.measureText(line+word).width>width&&line){ctx.fillText(line,x,y,width);y+=size*1.4;lines++;line='';}
        if(lines>=maxLines){truncated=true;break;}line+=word+' ';
      }
      if(lines>=maxLines){truncated=true;break;}ctx.fillText(line,x,y,width);y+=size*1.4;lines++;
    }
    if(truncated){ctx.fillText('…',x,y,width);status(i18n.t('Some text exceeds the image layout. Shorten it, or use the HTML album export to retain long stories.'));}
    return y;
  }
  /** The picture's size for this creation: the chosen shape for the ones that can take one, the sheet otherwise. */
  function sizeFor(current){
    if(SHAPED.includes(current))return FORMATS[$('[data-format]').value]||FORMATS.landscape;
    return [1440,current==='recipe'||current==='calendar'?1800:1080];
  }
  function paintMask(){
    const m=mask.getContext('2d');m.clearRect(0,0,mask.width,mask.height);
    for(const s of maskStrokes){m.globalCompositeOperation=s.erase?'destination-out':'source-over';m.strokeStyle='#fff';m.fillStyle='#fff';m.lineWidth=s.size;m.lineCap='round';m.lineJoin='round';m.beginPath();s.points.forEach((p,i)=>i?m.lineTo(p.x,p.y):m.moveTo(p.x,p.y));m.stroke();const p=s.points[0];m.beginPath();m.arc(p.x,p.y,s.size/2,0,Math.PI*2);m.fill();}m.globalCompositeOperation='source-over';
  }
  function render(){
    if(closed||!active())return;
    const current=mode(),image=active().image;
    const invalid=current==='compare'&&photos.length<2 || current==='calendar'&&!/^[1-9]\d{3}-(0[1-9]|1[0-2])$/.test($('[data-month]').value);
    $('[data-export]').disabled=invalid;$('[data-print]').disabled=invalid;
    if(invalid)status(current==='compare'?i18n.t('Add a second photo for a then-and-now comparison.'):i18n.t('Choose a valid calendar month before exporting.'));
    const [W,H]=sizeFor(current);
    if(canvas.width!==W)canvas.width=W;
    if(canvas.height!==H)canvas.height=H;
    if(mask.width!==W||mask.height!==H){mask.width=W;mask.height=H;}
    $('[data-save-library]').hidden=!(canSave&&sourceId&&PICTURES.includes(current));
    ctx.fillStyle=$('[data-paper]').value;ctx.fillRect(0,0,canvas.width,canvas.height);
    $('[data-pages]').replaceChildren();
    if(current==='restoration'||current==='compare'){
      const a=current==='restoration'?source:image,b=current==='restoration'?restored:(comparisonPhoto?.image||image);
      cover(ctx,b,0,0,1440,1080,'none',true);ctx.save();ctx.beginPath();ctx.rect(0,0,1440*Number($('[data-compare]').value)/100,1080);ctx.clip();cover(ctx,a,0,0,1440,1080);ctx.restore();
      ctx.fillStyle='#ffffff';ctx.fillRect(1440*Number($('[data-compare]').value)/100-2,0,4,1080);
    }else if(current==='selective'){
      cover(ctx,image,0,0,W,H,'grayscale(1)');paintMask();
      const layer=document.createElement('canvas');layer.width=W;layer.height=H;const l=layer.getContext('2d');cover(l,image,0,0,W,H);l.globalCompositeOperation='destination-in';l.drawImage(mask,0,0);ctx.drawImage(layer,0,0);
    }else if(current==='postcard'){
      // The photograph fills the card; the words sit on a band that darkens
      // towards the foot, so they read on any picture and the picture is not cut.
      cover(ctx,image,0,0,W,H);
      const band=Math.round(Math.min(H*0.42,320+H*0.12)),top=H-band;
      const shade=ctx.createLinearGradient(0,top,0,H);shade.addColorStop(0,'rgba(0,0,0,0)');shade.addColorStop(0.35,'rgba(0,0,0,0.45)');shade.addColorStop(1,'rgba(0,0,0,0.72)');
      ctx.fillStyle=shade;ctx.fillRect(0,top,W,band);
      const pad=Math.round(W*0.05),big=Math.round(Math.min(W,H)*0.055),small=Math.round(big*0.58);
      let y=text($('[data-title]').value,pad,top+band*0.3,W-2*pad,big,2,'#ffffff');
      text([active().date,active().caption,$('[data-story]').value].filter(Boolean).join(' · '),pad,y+small*0.3,W-2*pad,small,4,'#f3f3f3');
    }else if(current==='collage'||current==='versions'){
      const entries=current==='versions'?[{image,filter:$('[data-look]').value,name:i18n.t('Current look')},...versions]:photos;
      const layout=current==='collage'?$('[data-layout]').value:'grid';
      const gap=Number($('[data-gap]').value),titleSize=Math.round(Math.min(W,H)*0.04),head=Math.round(titleSize*2.4);
      text($('[data-title]').value,30,22,W-60,titleSize,1);
      const frame=(p,x,y,w,h,tilt=0)=>{
        ctx.save();
        if(layout==='polaroid'){
          // A print with its white border and a caption under it, each a little askew.
          ctx.translate(x+w/2,y+h/2);ctx.rotate(tilt);ctx.translate(-w/2,-h/2);
          ctx.shadowColor='rgba(0,0,0,0.25)';ctx.shadowBlur=18;ctx.shadowOffsetY=6;ctx.fillStyle='#ffffff';ctx.fillRect(0,0,w,h);ctx.shadowColor='transparent';
          const b=Math.round(w*0.06);cover(ctx,p.image,b,b,w-2*b,h-b-Math.round(h*0.16),p.filter||'none');
          ctx.font=`${Math.max(16,Math.round(w*0.055))}px system-ui`;ctx.fillStyle='#20232b';ctx.textBaseline='middle';ctx.textAlign='center';ctx.fillText(p.caption||p.name||'',w/2,h-Math.round(h*0.08),w-2*b);
        }else{
          cover(ctx,p.image,x,y,w,h-36,p.filter||'none');text(p.caption||p.name||'',x,y+h-32,w,22,1);
        }
        ctx.restore();
      };
      if(layout==='feature'&&entries.length>1){
        const bigW=Math.round((W-3*gap)*0.62),x1=gap*2+bigW,restW=W-x1-gap,rows=entries.length-1,h=(H-head-gap*(rows+1))/rows;
        frame(entries[0],gap,head+gap,bigW,H-head-2*gap);
        entries.slice(1).forEach((p,i)=>frame(p,x1,head+gap+i*(h+gap),restW,h));
      }else{
        const columns=Math.min(Number($('[data-columns]').value),Math.max(1,entries.length)),rows=Math.ceil(entries.length/columns),w=(W-gap*(columns+1))/columns,h=(H-head-gap*(rows+1))/rows;
        entries.forEach((p,i)=>{const x=gap+(i%columns)*(w+gap),y=head+gap+Math.floor(i/columns)*(h+gap);frame(p,x,y,w,h,layout==='polaroid'?((i%2?1:-1)*(0.02+0.01*(i%3))):0);});
      }
    }else if(current==='calendar'){
      cover(ctx,image,0,0,1440,680);const [year,month]=$('[data-month]').value.split('-').map(Number);
      if(!year||!month)return;
      text(new Date(year,month-1,1).toLocaleDateString(i18n.locale(),{month:'long',year:'numeric'}),60,710,1320,60,1);
      const first=new Date(year,month-1,1).getDay(),days=new Date(year,month,0).getDate();
      // Weekday names from the locale: 4–10 January 1970 ran Sunday to Saturday.
      [4,5,6,7,8,9,10].map(d=>new Date(1970,0,d).toLocaleDateString(i18n.locale(),{weekday:'short'})).forEach((day,i)=>text(day,60+i*190,815,180,28,1));
      for(let d=1;d<=days;d++){const cell=first+d-1;text(d,60+cell%7*190,880+Math.floor(cell/7)*96,180,34,1);}
      text($('[data-events]').value,60,1500,1320,28,6);
    }else if(current==='recipe'){
      cover(ctx,image,0,0,1440,570);text($('[data-title]').value,60,610,1320,52,2);
      text($('[data-story]').value,60,755,1320,28,4);text(i18n.t('Ingredients'),60,960,600,36,1);text($('[data-ingredients]').value,60,1020,600,27,17);
      text(i18n.t('Directions'),760,960,620,36,1);text($('[data-directions]').value,760,1020,620,27,17);
    }else{
      const p=timer?photos[slide%photos.length]:active();cover(ctx,p.image,0,0,1440,780);text($('[data-title]').value,50,810,1340,46,1);
      text([p.date,p.caption,$('[data-story]').value].filter(Boolean).join(' · '),50,885,1340,30,4);
      if(current==='album'){for(const p of photos){const article=document.createElement('article'),img=document.createElement('img'),caption=document.createElement('p');img.src=p.data;img.alt=p.caption;caption.textContent=[p.date,p.caption].filter(Boolean).join(' · ');article.append(img,caption);$('[data-pages]').append(article);}}
    }
  }
  function list(){const select=$('[data-photo]'),old=select.value;select.replaceChildren();photos.forEach((p,i)=>{const o=document.createElement('option');o.value=i;o.textContent=`${i+1}. ${p.name}`;select.append(o);});select.value=Number(old)<photos.length?old:'0';if(!select.value)select.value='0';syncPhoto();}
  function syncPhoto(){$('[data-caption]').value=active()?.caption||'';$('[data-date]').value=active()?.date||'';stroke=null;maskStrokes=painted.get(active())||[];painted.set(active(),maskStrokes);syncSecond();render();}
  function syncSecond(){const select=$('[data-second]');select.replaceChildren();if(!photos.includes(comparisonPhoto)||comparisonPhoto===active())comparisonPhoto=photos.find(p=>p!==active())||null;photos.forEach((p,i)=>{if(p===active())return;const option=document.createElement('option');option.value=i;option.textContent=p.name;select.append(option);});select.value=String(photos.indexOf(comparisonPhoto));}
  function refresh(){stop();$('[data-picker]').hidden=true;dialog.querySelectorAll('[data-group]').forEach(el=>el.hidden=!el.dataset.group.split(' ').includes(mode()));$('[data-help]').textContent=i18n.t(modes[mode()][1]);$('[data-export]').textContent=['restoration','compare','album','slideshow'].includes(mode())?i18n.t('Download interactive HTML'):i18n.t('Download PNG');render();}
  async function makePhoto(blob,name){const b=await decode(blob);try{const c=document.createElement('canvas'),scale=Math.min(1,1600/Math.max(b.width,b.height));c.width=Math.round(b.width*scale);c.height=Math.round(b.height*scale);c.getContext('2d').drawImage(b,0,0,c.width,c.height);const data=c.toDataURL('image/jpeg',.92);const image=await createImageBitmap(c);return {image,data,name,caption:'',date:''};}finally{b.close();}}
  let source,restored;
  try{source=(await makePhoto(original,i18n.t('Original'))).image;const b=await makePhoto(edited,i18n.t('Edited photo'));photos=[b];restored=await createImageBitmap(b.image);list();refresh();status(i18n.t('Ready. Add photos or choose a creative format.'));}catch(error){source?.close();photos.forEach(p=>p.image.close());dialog.remove();throw error;}
  function close(){if(changed&&!window.confirm(i18n.t('Close Creative Studio? Undownloaded creations will be lost.')))return;dialog.close();}
  $('[data-close]').onclick=close;dialog.oncancel=e=>{e.preventDefault();close();};
  dialog.onclose=()=>{closed=true;audioRevision++;stop();if(audioURL)URL.revokeObjectURL(audioURL);source.close();restored.close();photos.forEach(p=>p.image.close());versions.forEach(p=>p.image.close());dialog.remove();if(returnFocus?.isConnected)returnFocus.focus();};
  // Stop gallery/editor shortcuts while this nested workspace owns the keyboard.
  dialog.addEventListener('keydown',e=>e.stopPropagation());
  dialog.addEventListener('input',e=>{if(e.target.matches('[data-files],[data-audio]'))return;changed=true;if(e.target.matches('[data-caption]'))active().caption=e.target.value;if(e.target.matches('[data-date]'))active().date=e.target.value;render();});
  $('[data-mode]').onchange=refresh;$('[data-photo]').onchange=()=>{stop();syncPhoto();};
  $('[data-second]').onchange=()=>{comparisonPhoto=photos[Number($('[data-second]').value)];render();};
  $('[data-files]').onchange=async e=>{
    if(busy)return;busy=true;e.target.disabled=true;
    try{for(const file of e.target.files){if(photos.length>=12){status(i18n.t('A creation supports up to 12 photos.'));break;}const p=await makePhoto(file,file.name);if(closed){p.image.close();break;}photos.push(p);changed=true;}if(!closed){list();status(photos.length===1?i18n.t('1 photo ready.'):i18n.t('{count} photos ready.',{count:photos.length}));}}
    catch(error){if(!closed){list();status(error.message);}}finally{busy=false;e.target.disabled=false;e.target.value='';}
  };
  $('[data-earlier]').onclick=()=>{const i=Number($('[data-photo]').value);if(i>0){[photos[i-1],photos[i]]=[photos[i],photos[i-1]];list();$('[data-photo]').value=i-1;syncPhoto();changed=true;}};
  $('[data-remove]').onclick=()=>{if(photos.length===1){status(i18n.t('Keep at least one photo.'));return;}photos.splice(Number($('[data-photo]').value),1)[0].image.close();list();changed=true;};
  $('[data-audio]').onchange=async e=>{const revision=++audioRevision,file=e.target.files[0];if(!file)return;if(!file.type.startsWith('audio/')||!file.size||file.size>15*1024*1024){status(i18n.t('Choose a nonempty audio file up to 15 MB.'));return;}try{const data=await readData(file);if(closed||revision!==audioRevision)return;stop();if(audioURL)URL.revokeObjectURL(audioURL);audio=data;audioURL=URL.createObjectURL(file);$('audio').src=audioURL;$('audio').hidden=false;changed=true;status(i18n.t('Audio attached. Use the player to check it.'));}catch(error){if(!closed&&revision===audioRevision)status(error.message);}};
  $('[data-clear-audio]').onclick=()=>{audioRevision++;stop();audio=null;if(audioURL)URL.revokeObjectURL(audioURL);audioURL=null;$('audio').removeAttribute('src');$('audio').load();$('audio').hidden=true;$('[data-audio]').value='';changed=true;};
  $('[data-play]').onclick=()=>{stop();slide=0;const seconds=Math.max(2,Math.min(30,Number($('[data-seconds]').value)||5));timer=setInterval(()=>{slide++;render();},seconds*1000);render();if(audio)$('audio').play().catch(()=>status(i18n.t('Use the audio player to start your soundtrack.')));};$('[data-stop]').onclick=()=>{stop();render();};
  function addPoint(event){const rect=canvas.getBoundingClientRect();return {x:(event.clientX-rect.left)*1440/rect.width,y:(event.clientY-rect.top)*canvas.height/rect.height};}
  canvas.onpointerdown=e=>{if(mode()!=='selective')return;e.preventDefault();canvas.setPointerCapture(e.pointerId);stroke={size:Number($('[data-brush]').value),erase:$('[data-erase]').checked,points:[addPoint(e)]};maskStrokes.push(stroke);changed=true;render();};
  canvas.onpointermove=e=>{if(stroke){stroke.points.push(addPoint(e));render();}};canvas.onpointerup=canvas.onpointercancel=()=>{stroke=null;};
  canvas.onkeydown=e=>{if(mode()!=='selective')return;if(e.key.startsWith('Arrow')){e.preventDefault();brushPoint.x=Math.max(0,Math.min(1440,brushPoint.x+({'ArrowRight':20,'ArrowLeft':-20}[e.key]||0)));brushPoint.y=Math.max(0,Math.min(1080,brushPoint.y+({'ArrowDown':20,'ArrowUp':-20}[e.key]||0)));status(i18n.t('Brush position {x}, {y}. Press Space to paint.',{x:brushPoint.x,y:brushPoint.y}));}if(e.code==='Space'){e.preventDefault();maskStrokes.push({size:Number($('[data-brush]').value),erase:$('[data-erase]').checked,points:[{...brushPoint}]});changed=true;render();}};
  $('[data-undo-paint]').onclick=()=>{maskStrokes.pop();render();changed=true;};$('[data-clear-paint]').onclick=()=>{maskStrokes.length=0;render();changed=true;};
  // A new shape is a new sheet of paper: what was painted on the old one is in its coordinates.
  $('[data-format]').onchange=()=>{for(const strokes of photos.map(p=>painted.get(p)).filter(Boolean))strokes.length=0;render();};

  // -- photographs from the library, not only from files ------------------------
  let pickerOffset=0;
  async function showPicker(more=false){
    const panel=$('[data-picker]'),grid=$('[data-picker-grid]');
    if(!more){if(!panel.hidden){panel.hidden=true;return;}pickerOffset=0;grid.replaceChildren();}
    panel.hidden=false;status(i18n.t('Looking in the library…'));
    try{
      const response=await fetch(`/api/assets?limit=48&offset=${pickerOffset}&kind=picture`,{credentials:'same-origin'});
      if(!response.ok)throw new Error(i18n.t('Could not reach the library.'));
      const items=((await response.json()).items||[]).filter(item=>item.kind==='picture'&&item.view);
      pickerOffset+=48;
      for(const item of items){
        const button=document.createElement('button');button.type='button';button.title=item.name||'';
        const img=document.createElement('img');img.src=thumbUrl(item.id,160,item.thumb_v);img.alt=item.name||'';img.loading='lazy';button.append(img);
        button.onclick=async()=>{
          if(busy)return;if(photos.length>=12){status(i18n.t('A creation supports up to 12 photos.'));return;}
          busy=true;button.disabled=true;
          try{const r=await fetch(item.view,{credentials:'same-origin'});if(!r.ok)throw new Error(i18n.t('Photo could not be loaded.'));const p=await makePhoto(await r.blob(),item.name||i18n.t('Library photo'));if(closed){p.image.close();return;}photos.push(p);changed=true;list();$('[data-photo]').value=String(photos.length-1);syncPhoto();status(i18n.t('Added from the library.'));}
          catch(error){if(!closed)status(error.message);}finally{busy=false;button.disabled=false;}
        };
        grid.append(button);
      }
      $('[data-picker-more]').hidden=items.length<48;
      status(grid.childElementCount?i18n.t('Choose a photograph to add.'):i18n.t('Nothing in the library to add.'));
    }catch(error){if(!closed)status(error.message);}
  }
  $('[data-library]').onclick=()=>showPicker(false);$('[data-picker-more]').onclick=()=>showPicker(true);

  // -- into the library, the way an edit goes ---------------------------------------
  $('[data-save-library]').onclick=()=>{
    stop();render();if(busy||!canSave||!sourceId)return;busy=true;$('[data-save-library]').disabled=true;status(i18n.t('Saving new library copy…'));
    canvas.toBlob(async blob=>{
      try{if(!blob)throw new Error(i18n.t('Export failed.'));const copy=await saveLibraryCopy(sourceId,blob);changed=false;status(copy.pending?i18n.t(copy.message):i18n.t('Copy saved to the library.'));onSaved?.(copy);}
      catch(error){if(!closed)status(error.message);}finally{busy=false;$('[data-save-library]').disabled=false;}
    },'image/png');
  };
  $('[data-capture]').onclick=async()=>{if(capturing)return;if(versions.length>=11){status(i18n.t('Keep up to 11 versions per sheet.'));return;}capturing=true;$('[data-capture]').disabled=true;const filter=$('[data-look]').value,name=$('[data-version-name]').value||i18n.t('Version {number}',{number:versions.length+1});try{const image=await createImageBitmap(active().image);if(closed){image.close();return;}versions.push({image,filter,name});changed=true;render();status(i18n.t('Version kept in this workspace. Download the sheet before closing.'));}catch(error){if(!closed)status(error.message);}finally{capturing=false;if(!closed)$('[data-capture]').disabled=false;}};
  $('[data-version-download]').onclick=()=>{const c=document.createElement('canvas');c.width=active().image.width;c.height=active().image.height;const x=c.getContext('2d');x.filter=$('[data-look]').value;x.drawImage(active().image,0,0);c.toBlob(blob=>blob&&download(blob,'ninaivu-creative-version.png'));};
  function documentHTML(){
    const title=escapeHTML($('[data-title]').value),story=escapeHTML($('[data-story]').value),current=mode();
    let content='';
    if(current==='restoration'||current==='compare'){
      const temp=document.createElement('canvas');temp.width=1440;temp.height=1080;const t=temp.getContext('2d');
      cover(t,current==='restoration'?source:active().image,0,0,1440,1080);const before=temp.toDataURL('image/jpeg',.92);
      cover(t,current==='restoration'?restored:(comparisonPhoto?.image||active().image),0,0,1440,1080,'none',true);
      const comparison=Number($('[data-compare]').value);
      content=`<div class="compare"><img alt="${escapeHTML(i18n.t('After'))}" src="${temp.toDataURL('image/jpeg',.92)}"><img id="before" style="clip-path:inset(0 ${100-comparison}% 0 0)" alt="${escapeHTML(i18n.t('Before'))}" src="${before}"></div><label>${escapeHTML(i18n.t('Before / after'))} <input id="comparison" type="range" min="0" max="100" value="${comparison}"></label><script>document.getElementById('comparison').oninput=e=>document.getElementById('before').style.clipPath='inset(0 '+(100-e.target.value)+'% 0 0)';<\/script>`;
    }else if(current==='album'||current==='slideshow'){
      content=photos.map((p,i)=>`<figure ${current==='slideshow'?`class="slide" ${i?'hidden':''}`:''}><img src="${p.data}" alt="${escapeHTML(p.caption)}"><figcaption>${escapeHTML([p.date,p.caption].filter(Boolean).join(' · '))}</figcaption></figure>`).join('');
      if(audio)content+=`<audio controls src="${audio}"></audio>`;
      if(current==='slideshow'){const seconds=Math.max(2,Math.min(30,Number($('[data-seconds]').value)||5));content+=`<div class="controls"><button id="previous">${escapeHTML(i18n.t('Previous'))}</button><button id="play">${escapeHTML(i18n.t('Play / pause'))}</button><button id="next">${escapeHTML(i18n.t('Next'))}</button></div><script>const slides=[...document.querySelectorAll('.slide')];let index=0,timer;function show(n){slides[index].hidden=true;index=(n+slides.length)%slides.length;slides[index].hidden=false;}function stop(){clearInterval(timer);timer=null;document.querySelector('audio')?.pause();}document.getElementById('previous').onclick=()=>show(index-1);document.getElementById('next').onclick=()=>show(index+1);document.getElementById('play').onclick=()=>{if(timer){stop();return;}timer=setInterval(()=>show(index+1),${seconds*1000});document.querySelector('audio')?.play().catch(()=>{});};document.addEventListener('visibilitychange',()=>{if(document.hidden)stop();});<\/script>`;}
    }else content=`<img alt="${title}" src="${canvas.toDataURL('image/png')}">`;
    return `<!doctype html><html lang="${i18n.language()}"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>${title}</title><style>body{margin:24px auto;padding:0 20px;max-width:1000px;background:#faf7f1;color:#20232b;font:18px/1.6 system-ui}h1{line-height:1.2}p,figcaption{white-space:pre-wrap;overflow-wrap:anywhere}img{display:block;max-width:100%;max-height:80vh;object-fit:contain;margin:auto}figure{margin:24px 0}button,input,audio{font:inherit;margin:8px;max-width:95%}.compare{position:relative}.compare img{width:100%;max-height:none}.compare #before{position:absolute;inset:0;clip-path:inset(0 50% 0 0)}.slide{overflow:hidden}.slide img{animation:drift ${Math.max(2,Math.min(30,Number($('[data-seconds]').value)||5))}s ease-in-out both}@keyframes drift{from{transform:scale(1)}to{transform:scale(1.04)}}[hidden]{display:none!important}@media(prefers-reduced-motion:reduce){.slide img{animation:none}}@media print{button,input,audio,.controls{display:none}figure{break-inside:avoid}img{max-height:85vh}}</style><h1>${title}</h1><p>${story}</p>${content}</html>`;
  }
  $('[data-export]').onclick=()=>{stop();render();if($('[data-export]').disabled)return;const creation=mode();if(['restoration','compare','album','slideshow'].includes(creation))download(new Blob([documentHTML()],{type:'text/html'}),`ninaivu-${creation}.html`);else canvas.toBlob(blob=>blob&&download(blob,`ninaivu-${creation}.png`));status(i18n.t('Download prepared. Keep the file to preserve this creation.'));};
  $('[data-print]').onclick=()=>{stop();render();const win=window.open('','_blank');if(!win){status(i18n.t('Allow a popup to open the print layout.'));return;}const printable=documentHTML().replace(/<script>[\s\S]*?<\/script>/g,'').replace(/<audio[\s\S]*?<\/audio>/g,'').replace('</style>','@media print{.slide[hidden]{display:block!important}}</style>');win.document.open();win.document.write(printable);win.document.close();win.opener=null;Promise.all([...win.document.images].map(img=>img.decode().catch(()=>{}))).then(()=>{if(!win.closed){win.focus();win.print();}});};
  const now=new Date();$('[data-month]').value=`${now.getFullYear()}-${String(now.getMonth()+1).padStart(2,'0')}`;
  dialog.showModal();
}
