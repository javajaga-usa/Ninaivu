import {decode} from './ai-playground/utils/files.mjs';
import {saveLibraryCopy} from './ai-playground/services/library.mjs';
import {api, thumbUrl} from './api.js';
import * as i18n from './i18n.js';

//: The creations that are one picture, and so can be saved into the library like an edit.
const PICTURES=['postcard','collage','selective','versions','recipe','calendar'];
//: The creations whose picture can be made in another shape: a square for a profile, a tall one for a story.
const SHAPED=['postcard','collage','selective','versions'];
const FORMATS={landscape:[1440,1080],square:[1080,1080],story:[1080,1920]};
//: The creations that are a web page rather than a picture.
const PAGES=['restoration','compare','album','slideshow'];
const MAX_PHOTOS=12,PICKER_PAGE=48;

const modes = {
  restoration: [i18n.key('Restoration story'), i18n.key('Compare your original with the edited photo. Export a self-contained interactive story.')],
  album: [i18n.key('Family story album'), i18n.key('Add photos, dates and captions. Attach a recorded memory to your album.')],
  postcard: [i18n.key('Photo postcard'), i18n.key('Create a photo card with a title and personal message.')],
  collage: [i18n.key('Collage studio'), i18n.key('Arrange up to 12 photos; change columns, spacing and background.')],
  compare: [i18n.key('Side by side'), i18n.key('Add a second photo, then align it with zoom and horizontal/vertical position.')],
  slideshow: [i18n.key('Cinematic slideshow'), i18n.key('Play your photos with gentle motion, captions and optional music. Export an offline HTML presentation.')],
  selective: [i18n.key('Selective color'), i18n.key('Paint on the photo to reveal color against a monochrome background.')],
  versions: [i18n.key('Creative versions'), i18n.key('Keep named variations of your current photo. Export a comparison sheet or any individual version.')],
  recipe: [i18n.key('Recipe memories'), i18n.key('Pair a dish photo with ingredients, directions and the story behind it.')],
  calendar: [i18n.key('Photo calendar'), i18n.key('Create a printable month with a favorite photo and dated family occasions.')],
};
//: Line drawings in the manner of Sudar's and the rest of Ninaivu's.
const ICONS = {
  restoration: '<path d="m4 20 11-11"/><path d="m14 3.5.8 2 2 .8-2 .8-.8 2-.8-2-2-.8 2-.8Z"/><path d="M19 11v3M17.5 12.5h3"/>',
  album: '<path d="M4 5.5A1.5 1.5 0 0 1 5.5 4H11v16H5.5A1.5 1.5 0 0 1 4 18.5Z"/><path d="M20 5.5A1.5 1.5 0 0 0 18.5 4H13v16h5.5a1.5 1.5 0 0 0 1.5-1.5Z"/>',
  postcard: '<rect x="3" y="5" width="18" height="14" rx="2"/><path d="M10 5v14M13.5 9h4M13.5 12h4M13.5 15h2.5"/>',
  collage: '<rect x="3.5" y="3.5" width="7" height="9" rx="1.5"/><rect x="13.5" y="3.5" width="7" height="5" rx="1.5"/><rect x="13.5" y="11.5" width="7" height="9" rx="1.5"/><rect x="3.5" y="15.5" width="7" height="5" rx="1.5"/>',
  compare: '<rect x="3.5" y="4.5" width="17" height="15" rx="2"/><path d="M12 2.5v19M8.5 10l-2 2 2 2M15.5 10l2 2-2 2"/>',
  slideshow: '<rect x="3.5" y="4.5" width="17" height="12" rx="2"/><path d="m10.5 8 4 2.5-4 2.5ZM8 20h8"/>',
  selective: '<path d="m14.5 4.5 5 5L11 18l-5-5Z"/><path d="M6 13c-2 .5-2.5 2.5-2.5 4.5V20H6c2 0 4-.5 4.5-2.5"/>',
  versions: '<path d="m12 4 8.5 4.5L12 13 3.5 8.5Z"/><path d="m3.5 12.5 8.5 4.5 8.5-4.5M3.5 16.5 12 21l8.5-4.5"/>',
  recipe: '<path d="M3.5 11h17a8.5 8.5 0 0 1-17 0Z"/><path d="M9 7.5c0-1.5 1-2 1-3.5M13.5 7.5c0-1.5 1-2 1-3.5"/>',
  calendar: '<rect x="3.5" y="5" width="17" height="15" rx="2"/><path d="M3.5 10h17M8 3v4M16 3v4"/>',
  close: '<path d="M6 6l12 12M18 6 6 18"/>',
  library: '<rect x="3.5" y="3.5" width="17" height="17" rx="2.5"/><circle cx="9" cy="9" r="1.75"/><path d="m20.5 15-4.5-4.5L5.5 20.5"/>',
  upload: '<path d="M12 15.5V4M7.5 8.5 12 4l4.5 4.5M4.5 15v3a2 2 0 0 0 2 2h11a2 2 0 0 0 2-2v-3"/>',
  earlier: '<path d="M15 6 9 12l6 6"/>',
  later: '<path d="m9 6 6 6-6 6"/>',
  remove: '<path d="M4.5 7h15M9.5 7V4.5h5V7M6.5 7l1 12.5h9l1-12.5"/>',
  back: '<path d="M19 12H5M11 6l-6 6 6 6"/>',
};
const icon=name=>`<svg class="cs-icon" viewBox="0 0 24 24" aria-hidden="true">${ICONS[name]}</svg>`;
const escapeHTML = text => String(text).replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
const readData = blob => new Promise((resolve,reject) => {const reader=new FileReader();reader.onload=()=>resolve(reader.result);reader.onerror=()=>reject(new Error(i18n.t('Could not read file.')));reader.readAsDataURL(blob);});
const download = (blob,name) => {const url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download=name;a.click();setTimeout(()=>URL.revokeObjectURL(url),30000);};
const ISO_DATE=/^(\d{4})-(\d{2})-(\d{2})$/;
/** A day as the grid and the viewer write it, "15 January 2019" or its Tamil; anything else as it was typed. */
function dateText(value){
  const m=ISO_DATE.exec(value||'');if(!m)return value||'';
  try{return new Date(Number(m[1]),Number(m[2])-1,Number(m[3])).toLocaleDateString(i18n.locale(),{day:'numeric',month:'long',year:'numeric'});}catch{return value;}
}
/** What the library already knows about a photograph: its day and its caption, as the viewer shows them. */
const fromLibrary=item=>({date:ISO_DATE.test(item?.date||'')?item.date:'',caption:String(item?.caption||'').slice(0,180)});

/**
 * Creative Studio, opened two ways: from Sudar with the photo being edited
 * (`original` and `edited`, and a restoration story between them), or from
 * the gallery with photographs of the library (`ids`, a selection or the
 * photo in the viewer; or `filters`, an album, a trip or a person, of which
 * the first twelve photographs). Opened with neither, it starts at the
 * library picker.
 */
export async function openCreativeStudio({original=null, edited=null, ids=null, filters=null, returnFocus=document.activeElement, sourceId=null, canSave=false, onSaved=null}) {
  if(document.getElementById('creative-studio'))return;
  // A restoration story is the edit beside its original: only Sudar has both.
  const editing=!!(original&&edited);
  const offered=Object.keys(modes).filter(id=>editing||id!=='restoration');
  if(!document.getElementById('creative-style')){const link=document.createElement('link');link.id='creative-style';link.rel='stylesheet';link.href='/static/css/creative-studio.css';document.head.append(link);}
  const dialog=document.createElement('dialog');dialog.id='creative-studio';dialog.setAttribute('aria-labelledby','cs-title');
  // The dialog is modal, so the language cannot change while it is open: its
  // words are translated once, here, as it is built. `a()` is for a
  // translation that lands inside an attribute.
  const a=escapeHTML;
  const cards=offered.map(id=>[id,modes[id]]).map(([id,[name]],i)=>`<button type="button" class="cs-mode" role="radio" data-mode="${id}" aria-checked="${i?'false':'true'}" tabindex="${i?'-1':'0'}">${icon(id)}<span>${i18n.t(name)}</span></button>`).join('');
  dialog.innerHTML=`<header class="cs-header"><div class="cs-brand"><span class="cs-mark" aria-hidden="true">${icon('collage')}</span><h1 id="cs-title">${i18n.t('Creative Studio')} <span class="cs-private">${i18n.t('In your browser')}</span></h1></div>
    <button type="button" class="cs-close" data-close aria-label="${a(i18n.t('Close'))}" title="${a(i18n.t('Close'))}">${icon('close')}</button></header>
    <div class="cs-body"><main class="cs-stage"><div class="cs-canvas"><canvas width="1440" height="1080" tabindex="0" aria-label="${a(i18n.t('Creation preview; selective color brush supports arrow keys and Space'))}"></canvas></div><div class="cs-pages" data-pages></div><p role="status" class="cs-status" data-status>${i18n.t('Preparing your photos…')}</p></main>
    <aside class="cs-rail"><div class="cs-rail-body">
    <section><h2 class="cs-group-title" id="cs-create">${i18n.t('Create')}</h2><div class="cs-modes" role="radiogroup" aria-labelledby="cs-create">${cards}</div><p class="cs-help" data-help></p></section>
    <section><div class="cs-section-head"><h2 class="cs-group-title">${i18n.t('Photos')}</h2><div class="cs-tools"><button type="button" data-earlier title="${a(i18n.t('Move earlier'))}" aria-label="${a(i18n.t('Move earlier'))}">${icon('earlier')}</button><button type="button" data-later title="${a(i18n.t('Move later'))}" aria-label="${a(i18n.t('Move later'))}">${icon('later')}</button><button type="button" data-remove title="${a(i18n.t('Remove photo'))}" aria-label="${a(i18n.t('Remove photo'))}">${icon('remove')}</button></div></div><div class="cs-strip" data-strip></div>
    <div class="cs-row cs-add"><button type="button" data-library aria-expanded="false">${icon('library')}<span>${i18n.t('Add from library')}</span></button><label class="cs-file" title="${a(i18n.t('Add photos (JPEG, PNG, WebP)'))}">${icon('upload')}<span>${i18n.t('Add photos')}</span><input data-files type="file" accept="image/jpeg,image/png,image/webp" multiple aria-label="${a(i18n.t('Add photos (JPEG, PNG, WebP)'))}"></label></div>
    <div class="cs-picker-panel" data-picker hidden><div class="cs-tabs" role="tablist">${[['recent',i18n.t('Recent')],['favorites',i18n.t('Favourites')],['albums',i18n.t('Albums')],['people',i18n.t('People')]].map(([id,name],i)=>`<button type="button" role="tab" data-source="${id}" aria-selected="${i?'false':'true'}">${name}</button>`).join('')}</div>
    <input class="cs-search" data-picker-search type="search" enterkeyhint="search" placeholder="${a(i18n.t('Search — try “last summer”'))}" aria-label="${a(i18n.t('Search'))}">
    <div class="cs-picker-head" data-picker-head hidden><button type="button" data-picker-back title="${a(i18n.t('Back'))}" aria-label="${a(i18n.t('Back'))}">${icon('back')}</button><span data-picker-title></span></div>
    <div class="cs-picker" data-picker-grid></div><button type="button" class="cs-more" data-picker-more hidden>${i18n.t('More from the library')}</button></div>
    <label>${i18n.t('Photo caption')}<input data-caption maxlength="180"></label><label>${i18n.t('Photo date')}<input data-date type="date"></label>
    <label data-group="compare">${i18n.t('Compare with')}<select data-second></select></label></section>
    <section><h2 class="cs-group-title">${i18n.t('Words')}</h2><label>${i18n.t('Title')}<input data-title maxlength="90" value="${a(i18n.t('Our family memories'))}"></label>
    <label>${i18n.t('Message / story')}<textarea data-story rows="3" maxlength="1800" placeholder="${a(i18n.t('A memory worth keeping…'))}"></textarea></label></section>
    <section data-group="postcard collage selective versions recipe calendar"><h2 class="cs-group-title">${i18n.t('Layout')}</h2>
    <label data-group="postcard collage selective versions">${i18n.t('Format')}<select data-format><option value="landscape">${i18n.t('Landscape · 4:3')}</option><option value="square">${i18n.t('Square · 1:1')}</option><option value="story">${i18n.t('Story · 9:16')}</option></select></label>
    <label data-group="collage">${i18n.t('Layout')}<select data-layout><option value="grid">${i18n.t('Grid')}</option><option value="feature">${i18n.t('One large, the rest beside it')}</option><option value="polaroid">${i18n.t('Polaroid frames')}</option></select></label>
    <div data-group="collage versions"><label>${i18n.t('Columns')}<select data-columns><option>2</option><option>3</option><option>4</option></select></label><label>${i18n.t('Spacing')}<input data-gap type="range" min="0" max="40" value="16"></label></div>
    <label data-group="postcard collage recipe calendar versions" class="cs-colour">${i18n.t('Background')}<input data-paper type="color" value="#fff8eb"></label></section>
    <section data-group="restoration compare"><h2 class="cs-group-title">${i18n.t('Comparison')}</h2><label>${i18n.t('Before / after')}<input data-compare type="range" min="0" max="100" value="50"></label><div data-group="compare"><label>${i18n.t('Second photo zoom')}<input data-zoom type="range" min="1" max="3" step="0.05" value="1"></label><label>${i18n.t('Horizontal position')}<input data-x type="range" min="-100" max="100" value="0"></label><label>${i18n.t('Vertical position')}<input data-y type="range" min="-100" max="100" value="0"></label></div></section>
    <section data-group="album slideshow"><h2 class="cs-group-title">${i18n.t('Sound and timing')}</h2><label>${i18n.t('Music or recorded memory (up to 15 MB)')}<input data-audio type="file" accept="audio/*"></label><audio controls hidden></audio><div class="cs-row"><button type="button" data-clear-audio>${i18n.t('Remove audio')}</button></div><label>${i18n.t('Seconds per slide')}<input data-seconds type="number" min="2" max="30" value="5"></label><div class="cs-row"><button type="button" data-play>${i18n.t('Play preview')}</button><button type="button" data-stop>${i18n.t('Stop preview')}</button></div></section>
    <section data-group="selective"><h2 class="cs-group-title">${i18n.t('Brush')}</h2><label>${i18n.t('Brush size')}<input data-brush type="range" min="8" max="180" value="60"></label><label class="cs-check"><input data-erase type="checkbox">${i18n.t('Erase color')}</label><div class="cs-row"><button type="button" data-undo-paint>${i18n.t('Undo brush stroke')}</button><button type="button" data-clear-paint>${i18n.t('Reset mask')}</button></div><p class="cs-help">${i18n.t('Use a mouse or touch. Keyboard: focus the canvas, move the brush with arrow keys, then press Space to paint.')}</p></section>
    <section data-group="versions"><h2 class="cs-group-title">${i18n.t('Looks')}</h2><label>${i18n.t('Look')}<select data-look><option value="none">${i18n.t('Natural')}</option><option value="sepia(.45) saturate(1.25)">${i18n.t('Warm memory')}</option><option value="grayscale(1) contrast(1.15)">${i18n.t('Monochrome')}</option><option value="saturate(.7) contrast(1.2)">${i18n.t('Cinema')}</option></select></label><label>${i18n.t('Version name')}<input data-version-name maxlength="60" value="${a(i18n.t('My look'))}"></label><div class="cs-row"><button type="button" data-capture>${i18n.t('Keep version')}</button><button type="button" data-version-download>${i18n.t('Download this look')}</button></div></section>
    <section data-group="recipe"><h2 class="cs-group-title">${i18n.t('Recipe memories')}</h2><label>${i18n.t('Ingredients')}<textarea data-ingredients rows="4" maxlength="1600"></textarea></label><label>${i18n.t('Directions')}<textarea data-directions rows="4" maxlength="2200"></textarea></label></section>
    <section data-group="calendar"><h2 class="cs-group-title">${i18n.t('Photo calendar')}</h2><label>${i18n.t('Month')}<input data-month type="month"></label><label>${i18n.t('Occasions (one per line: day — event)')}<textarea data-events rows="4" maxlength="900" placeholder="${a(i18n.t('12 — Grandma’s birthday'))}"></textarea></label></section>
    </div><footer class="cs-foot"><button type="button" class="btn primary" data-export>${i18n.t('Export creation')}</button><div class="cs-row"><button type="button" data-save-library hidden>${i18n.t('Save to library')}</button><button type="button" data-print>${i18n.t('Print / save PDF')}</button></div><p class="cs-note">${i18n.t('Works in your browser. Exports contain the photos and audio you include. Originals are unchanged. Download before closing.')}</p></footer></aside></div>`;
  document.body.append(dialog);
  const $=selector=>dialog.querySelector(selector),canvas=$('canvas'),ctx=canvas.getContext('2d');
  let photos=[],versions=[],closed=false,busy=false,changed=false,audio=null,audioURL=null,audioRevision=0,timer=null,slide=0,maskStrokes=[],stroke=null,brushPoint={x:720,y:540};
  const painted=new WeakMap();
  let capturing=false;
  let comparisonPhoto=null;
  let current=editing?'restoration':(ids?.length===1?'postcard':'collage'),activeIndex=0;
  const mask=document.createElement('canvas');mask.width=1440;mask.height=1080;
  // Where the library picker is looking: a tab, an album or a person in it, and any search.
  const picker={source:'recent',album:null,person:null,q:'',offset:0,revision:0};
  const pickerGrid=$('[data-picker-grid]');
  // The app's own type, Tamil face first: words drawn into a picture read
  // as they do everywhere else in Ninaivu, not in whatever the canvas picks.
  const FONT=(getComputedStyle(document.documentElement).getPropertyValue('--font')||'').trim()||'system-ui, sans-serif';
  const fontAsked=new Set();
  const mode=()=>current;
  const active=()=>photos[activeIndex]||photos[0];
  const status=text=>{$('[data-status]').textContent=text;};
  function stop(){clearInterval(timer);timer=null;$('audio').pause();}
  function cover(context,image,x,y,w,h,filter='none',align=false){
    const scale=Math.max(w/image.width,h/image.height)*(align?Number($('[data-zoom]').value):1);
    const dw=image.width*scale,dh=image.height*scale;
    const dx=align?Number($('[data-x]').value)*(dw-w)/200:0,dy=align?Number($('[data-y]').value)*(dh-h)/200:0;
    context.save();context.beginPath();context.rect(x,y,w,h);context.clip();context.filter=filter;context.drawImage(image,x+(w-dw)/2+dx,y+(h-dh)/2+dy,dw,dh);context.restore();
  }
  /** A face the page has not needed yet (Tamil, say) is fetched once, and the picture drawn again when it arrives. */
  function wantFont(font,value){
    if(!document.fonts||!value)return;
    const key=font+'\u0000'+value;if(fontAsked.has(key))return;fontAsked.add(key);
    try{if(!document.fonts.check(font,value))document.fonts.load(font,value).then(()=>{if(!closed)render();},()=>{});}catch{/* an old browser: the canvas's own face */}
  }
  function text(value,x,y,width,size=32,maxLines=6,colour='#20232b',weight=400){
    ctx.font=`${weight} ${size}px ${FONT}`;ctx.fillStyle=colour;ctx.textBaseline='top';wantFont(ctx.font,String(value));
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
  const captionOf=p=>[dateText(p.date),p.caption].filter(Boolean).join(' · ');
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
    if(closed)return;
    if(!active()){$('[data-export]').disabled=true;$('[data-print]').disabled=true;$('[data-save-library]').hidden=true;return;}
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
      cover(ctx,b,0,0,1440,1080,'none',current==='compare');ctx.save();ctx.beginPath();ctx.rect(0,0,1440*Number($('[data-compare]').value)/100,1080);ctx.clip();cover(ctx,a,0,0,1440,1080);ctx.restore();
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
      let y=text($('[data-title]').value,pad,top+band*0.3,W-2*pad,big,2,'#ffffff',650);
      text([captionOf(active()),$('[data-story]').value].filter(Boolean).join(' · '),pad,y+small*0.3,W-2*pad,small,4,'#f3f3f3');
    }else if(current==='collage'||current==='versions'){
      const entries=current==='versions'?[{image,filter:$('[data-look]').value,name:i18n.t('Current look')},...versions]:photos;
      const layout=current==='collage'?$('[data-layout]').value:'grid';
      const gap=Number($('[data-gap]').value),titleSize=Math.round(Math.min(W,H)*0.04),head=Math.round(titleSize*2.4);
      text($('[data-title]').value,30,22,W-60,titleSize,1,'#20232b',650);
      const frame=(p,x,y,w,h,tilt=0)=>{
        ctx.save();
        if(layout==='polaroid'){
          // A print with its white border and a caption under it, each a little askew.
          ctx.translate(x+w/2,y+h/2);ctx.rotate(tilt);ctx.translate(-w/2,-h/2);
          ctx.shadowColor='rgba(0,0,0,0.25)';ctx.shadowBlur=18;ctx.shadowOffsetY=6;ctx.fillStyle='#ffffff';ctx.fillRect(0,0,w,h);ctx.shadowColor='transparent';
          const b=Math.round(w*0.06);cover(ctx,p.image,b,b,w-2*b,h-b-Math.round(h*0.16),p.filter||'none');
          ctx.font=`400 ${Math.max(16,Math.round(w*0.055))}px ${FONT}`;ctx.fillStyle='#20232b';ctx.textBaseline='middle';ctx.textAlign='center';wantFont(ctx.font,p.caption||p.name||'');ctx.fillText(p.caption||p.name||'',w/2,h-Math.round(h*0.08),w-2*b);
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
      text(new Date(year,month-1,1).toLocaleDateString(i18n.locale(),{month:'long',year:'numeric'}),60,710,1320,60,1,'#20232b',650);
      const first=new Date(year,month-1,1).getDay(),days=new Date(year,month,0).getDate();
      // Weekday names from the locale: 4–10 January 1970 ran Sunday to Saturday.
      [4,5,6,7,8,9,10].map(d=>new Date(1970,0,d).toLocaleDateString(i18n.locale(),{weekday:'short'})).forEach((day,i)=>text(day,60+i*190,815,180,28,1));
      for(let d=1;d<=days;d++){const cell=first+d-1;text(d,60+cell%7*190,880+Math.floor(cell/7)*96,180,34,1);}
      text($('[data-events]').value,60,1500,1320,28,6);
    }else if(current==='recipe'){
      cover(ctx,image,0,0,1440,570);text($('[data-title]').value,60,610,1320,52,2,'#20232b',650);
      text($('[data-story]').value,60,755,1320,28,4);text(i18n.t('Ingredients'),60,960,600,36,1,'#20232b',650);text($('[data-ingredients]').value,60,1020,600,27,17);
      text(i18n.t('Directions'),760,960,620,36,1,'#20232b',650);text($('[data-directions]').value,760,1020,620,27,17);
    }else{
      const p=timer?photos[slide%photos.length]:active();cover(ctx,p.image,0,0,1440,780);text($('[data-title]').value,50,810,1340,46,1,'#20232b',650);
      text([captionOf(p),$('[data-story]').value].filter(Boolean).join(' · '),50,885,1340,30,4);
      if(current==='album'){for(const p of photos){const article=document.createElement('article'),img=document.createElement('img'),caption=document.createElement('p');img.src=p.data;img.alt=p.caption;caption.textContent=captionOf(p);article.append(img,caption);$('[data-pages]').append(article);}}
    }
  }
  // -- the creation's photographs, as a row of pictures ------------------------
  function list(){
    if(activeIndex>=photos.length)activeIndex=Math.max(0,photos.length-1);
    const strip=$('[data-strip]');strip.replaceChildren();
    photos.forEach((p,i)=>{
      const b=document.createElement('button');b.type='button';b.className='cs-thumb';b.setAttribute('aria-pressed',String(i===activeIndex));b.title=p.name;b.setAttribute('aria-label',`${i+1}. ${p.name}`);
      const img=document.createElement('img');img.src=p.thumb;img.alt='';const n=document.createElement('span');n.textContent=String(i+1);
      b.append(img,n);b.onclick=()=>{stop();choose(i);};strip.append(b);
    });
    $('[data-earlier]').disabled=activeIndex===0;$('[data-later]').disabled=activeIndex>=photos.length-1;$('[data-remove]').disabled=photos.length<2;
    markPicked();syncPhoto();
  }
  function choose(i){activeIndex=i;list();dialog.querySelectorAll('.cs-thumb')[i]?.focus();}
  function syncPhoto(){$('[data-caption]').value=active()?.caption||'';$('[data-date]').value=active()?.date||'';stroke=null;if(!active()){syncSecond();render();return;}maskStrokes=painted.get(active())||[];painted.set(active(),maskStrokes);syncSecond();render();}
  function syncSecond(){const select=$('[data-second]');select.replaceChildren();if(!photos.includes(comparisonPhoto)||comparisonPhoto===active())comparisonPhoto=photos.find(p=>p!==active())||null;photos.forEach((p,i)=>{if(p===active())return;const option=document.createElement('option');option.value=i;option.textContent=`${i+1}. ${p.name}`;select.append(option);});select.value=String(photos.indexOf(comparisonPhoto));}
  function refresh(){
    stop();
    dialog.querySelectorAll('.cs-mode').forEach(card=>{const on=card.dataset.mode===mode();card.setAttribute('aria-checked',String(on));card.tabIndex=on?0:-1;});
    dialog.querySelectorAll('[data-group]').forEach(el=>el.hidden=!el.dataset.group.split(' ').includes(mode()));
    $('[data-help]').textContent=i18n.t(modes[mode()][1]);$('[data-export]').textContent=PAGES.includes(mode())?i18n.t('Download interactive HTML'):i18n.t('Download PNG');render();
  }
  function setMode(id){if(!modes[id]||id===current)return;current=id;refresh();}
  async function makePhoto(blob,name,known={}){const b=await decode(blob);try{const c=document.createElement('canvas'),scale=Math.min(1,1600/Math.max(b.width,b.height));c.width=Math.round(b.width*scale);c.height=Math.round(b.height*scale);c.getContext('2d').drawImage(b,0,0,c.width,c.height);const data=c.toDataURL('image/jpeg',.92);const s=document.createElement('canvas'),small=Math.min(1,160/Math.max(c.width,c.height));s.width=Math.max(1,Math.round(c.width*small));s.height=Math.max(1,Math.round(c.height*small));s.getContext('2d').drawImage(c,0,0,s.width,s.height);const image=await createImageBitmap(c);return {image,data,thumb:s.toDataURL('image/jpeg',.8),name,caption:known.caption||'',date:known.date||'',libraryId:known.libraryId??null};}finally{b.close();}}
  let source=null,restored=null;
  if(editing)try{source=(await makePhoto(original,i18n.t('Original'))).image;const b=await makePhoto(edited,i18n.t('Edited photo'),{libraryId:sourceId});photos=[b];restored=await createImageBitmap(b.image);list();refresh();status(i18n.t('Ready. Add photos or choose a creative format.'));}catch(error){source?.close();photos.forEach(p=>p.image.close());dialog.remove();throw error;}
  else{list();refresh();}
  // The photograph being edited carries its day and caption from the library,
  // as the viewer shows them, unless somebody has already typed their own.
  if(sourceId){
    api.asset(sourceId).then(item=>{
      const first=photos.find(p=>p.libraryId===sourceId);if(closed||!first)return;
      const known=fromLibrary(item);if(!first.caption)first.caption=known.caption;if(!first.date)first.date=known.date;
      if(item?.name)first.name=item.name;list();
    }).catch(()=>{/* the studio works without it */});
  }
  function close(){if(changed&&!window.confirm(i18n.t('Close Creative Studio? Undownloaded creations will be lost.')))return;dialog.close();}
  $('[data-close]').onclick=close;dialog.oncancel=e=>{e.preventDefault();close();};
  dialog.onclose=()=>{closed=true;audioRevision++;stop();if(audioURL)URL.revokeObjectURL(audioURL);source?.close();restored?.close();photos.forEach(p=>p.image.close());versions.forEach(p=>p.image.close());dialog.remove();if(returnFocus?.isConnected)returnFocus.focus();};
  // Stop gallery/editor shortcuts while this nested workspace owns the keyboard.
  dialog.addEventListener('keydown',e=>e.stopPropagation());
  dialog.addEventListener('input',e=>{if(e.target.matches('[data-files],[data-audio],[data-picker-search]'))return;changed=true;if(e.target.matches('[data-caption]')&&active())active().caption=e.target.value;if(e.target.matches('[data-date]')&&active())active().date=e.target.value;render();});
  const cardsEl=$('.cs-modes');
  cardsEl.onclick=e=>{const card=e.target.closest('.cs-mode');if(card)setMode(card.dataset.mode);};
  // A radio group: the arrows move along it and choose as they go.
  cardsEl.onkeydown=e=>{
    const step={ArrowRight:1,ArrowDown:1,ArrowLeft:-1,ArrowUp:-1}[e.key];if(!step&&e.key!=='Home'&&e.key!=='End')return;e.preventDefault();
    const i=offered.indexOf(current),next=e.key==='Home'?0:e.key==='End'?offered.length-1:(i+step+offered.length)%offered.length;
    setMode(offered[next]);const card=cardsEl.querySelector(`[data-mode="${offered[next]}"]`);card.focus();card.scrollIntoView({block:'nearest',inline:'nearest'});
  };
  $('[data-second]').onchange=()=>{comparisonPhoto=photos[Number($('[data-second]').value)];render();};
  $('[data-files]').onchange=async e=>{
    if(busy)return;busy=true;e.target.disabled=true;
    try{for(const file of e.target.files){if(photos.length>=MAX_PHOTOS){status(i18n.t('A creation supports up to 12 photos.'));break;}const p=await makePhoto(file,file.name);if(closed){p.image.close();break;}photos.push(p);changed=true;}if(!closed){list();status(photos.length===1?i18n.t('1 photo ready.'):i18n.t('{count} photos ready.',{count:photos.length}));}}
    catch(error){if(!closed){list();status(error.message);}}finally{busy=false;e.target.disabled=false;e.target.value='';}
  };
  function move(by){const i=activeIndex,j=i+by;if(j<0||j>=photos.length)return;[photos[j],photos[i]]=[photos[i],photos[j]];changed=true;choose(j);}
  $('[data-earlier]').onclick=()=>move(-1);$('[data-later]').onclick=()=>move(1);
  function removeAt(i){if(photos.length===1){status(i18n.t('Keep at least one photo.'));return false;}photos.splice(i,1)[0].image.close();if(activeIndex>i||activeIndex>=photos.length)activeIndex=Math.max(0,activeIndex-1);changed=true;list();return true;}
  $('[data-remove]').onclick=()=>removeAt(activeIndex);
  $('[data-audio]').onchange=async e=>{const revision=++audioRevision,file=e.target.files[0];if(!file)return;if(!file.type.startsWith('audio/')||!file.size||file.size>15*1024*1024){status(i18n.t('Choose a nonempty audio file up to 15 MB.'));return;}try{const data=await readData(file);if(closed||revision!==audioRevision)return;stop();if(audioURL)URL.revokeObjectURL(audioURL);audio=data;audioURL=URL.createObjectURL(file);$('audio').src=audioURL;$('audio').hidden=false;changed=true;status(i18n.t('Audio attached. Use the player to check it.'));}catch(error){if(!closed&&revision===audioRevision)status(error.message);}};
  $('[data-clear-audio]').onclick=()=>{audioRevision++;stop();audio=null;if(audioURL)URL.revokeObjectURL(audioURL);audioURL=null;$('audio').removeAttribute('src');$('audio').load();$('audio').hidden=true;$('[data-audio]').value='';changed=true;};
  $('[data-play]').onclick=()=>{stop();slide=0;const seconds=Math.max(2,Math.min(30,Number($('[data-seconds]').value)||5));timer=setInterval(()=>{slide++;render();},seconds*1000);render();if(audio)$('audio').play().catch(()=>status(i18n.t('Use the audio player to start your soundtrack.')));};$('[data-stop]').onclick=()=>{stop();render();};
  function addPoint(event){const rect=canvas.getBoundingClientRect();return {x:(event.clientX-rect.left)*canvas.width/rect.width,y:(event.clientY-rect.top)*canvas.height/rect.height};}
  canvas.onpointerdown=e=>{if(mode()!=='selective')return;e.preventDefault();canvas.setPointerCapture(e.pointerId);stroke={size:Number($('[data-brush]').value),erase:$('[data-erase]').checked,points:[addPoint(e)]};maskStrokes.push(stroke);changed=true;render();};
  canvas.onpointermove=e=>{if(stroke){stroke.points.push(addPoint(e));render();}};canvas.onpointerup=canvas.onpointercancel=()=>{stroke=null;};
  canvas.onkeydown=e=>{if(mode()!=='selective')return;if(e.key.startsWith('Arrow')){e.preventDefault();brushPoint.x=Math.max(0,Math.min(canvas.width,brushPoint.x+({'ArrowRight':20,'ArrowLeft':-20}[e.key]||0)));brushPoint.y=Math.max(0,Math.min(canvas.height,brushPoint.y+({'ArrowDown':20,'ArrowUp':-20}[e.key]||0)));status(i18n.t('Brush position {x}, {y}. Press Space to paint.',{x:brushPoint.x,y:brushPoint.y}));}if(e.code==='Space'){e.preventDefault();maskStrokes.push({size:Number($('[data-brush]').value),erase:$('[data-erase]').checked,points:[{...brushPoint}]});changed=true;render();}};
  $('[data-undo-paint]').onclick=()=>{maskStrokes.pop();render();changed=true;};$('[data-clear-paint]').onclick=()=>{maskStrokes.length=0;render();changed=true;};
  // A new shape is a new sheet of paper: what was painted on the old one is in its coordinates.
  $('[data-format]').onchange=()=>{for(const strokes of photos.map(p=>painted.get(p)).filter(Boolean))strokes.length=0;render();};

  // -- photographs from the library, found the ways the grid finds them --------
  // Recent, favourites, an album or a person, or a search, through the app's
  // own requests: the same visibility, the same "only to me", the same
  // sign-in handling as the grid, and the day and caption the viewer shows.
  function markPicked(){const ids=new Set(photos.map(p=>p.libraryId).filter(id=>id!=null));pickerGrid.querySelectorAll('[data-asset]').forEach(b=>b.setAttribute('aria-pressed',String(ids.has(Number(b.dataset.asset)))));}
  function pickerFilters(){
    const filters={kinds:['picture'],q:picker.q};
    if(picker.source==='favorites')filters.favorites=true;
    if(picker.album)filters.album=picker.album.id;
    if(picker.person)filters.person=picker.person.id;
    return filters;
  }
  function showHead(title){$('[data-picker-head]').hidden=!title;$('[data-picker-title]').textContent=title||'';}
  async function showPhotos(more=false){
    const revision=++picker.revision;
    if(!more){picker.offset=0;pickerGrid.replaceChildren();pickerGrid.className='cs-picker';}
    $('[data-picker-more]').hidden=true;status(i18n.t('Looking in the library…'));
    try{
      const answer=await api.assets(pickerFilters(),{limit:PICKER_PAGE,offset:picker.offset});
      if(closed||revision!==picker.revision)return;
      const items=(answer.items||[]).filter(item=>item.kind==='picture'&&item.view);
      picker.offset+=PICKER_PAGE;
      for(const item of items){
        const button=document.createElement('button');button.type='button';button.dataset.asset=String(item.id);button.title=[item.name,dateText(item.date)].filter(Boolean).join(' · ');button.setAttribute('aria-label',button.title||i18n.t('Library photo'));
        const img=document.createElement('img');img.src=thumbUrl(item.id,160,item.thumb_v);img.alt='';img.loading='lazy';button.append(img);
        button.onclick=()=>pick(item,button);
        pickerGrid.append(button);
      }
      markPicked();
      $('[data-picker-more]').hidden=(answer.items||[]).length<PICKER_PAGE;
      status(pickerGrid.childElementCount?i18n.t('Choose a photograph to add.'):picker.q?i18n.t('Nothing matches'):picker.album||picker.person?i18n.t('No photos in here.'):i18n.t('Nothing in the library to add.'));
    }catch(error){if(!closed&&revision===picker.revision)status(error.message||i18n.t('Could not reach the library.'));}
  }
  /** Albums and people as a list of covers, the way Books offers them. */
  async function showGroups(kind){
    const revision=++picker.revision;pickerGrid.replaceChildren();pickerGrid.className='cs-picker cs-groups';$('[data-picker-more]').hidden=true;status(i18n.t('Looking in the library…'));
    try{
      const answer=kind==='albums'?await api.albums():await api.people();
      if(closed||revision!==picker.revision)return;
      const groups=kind==='albums'?(answer.albums||[]):(answer.people||[]);
      for(const group of groups){
        const count=Number(kind==='albums'?group.n:group.photo_count)||0;if(!count)continue;
        const button=document.createElement('button');button.type='button';button.className='cs-group';
        const cover=kind==='albums'?(group.cover_id?thumbUrl(group.cover_id,160):''):(group.cover_face_id?`/api/faces/thumb/${group.cover_face_id}`:'');
        const img=document.createElement('img');img.alt='';img.loading='lazy';if(cover)img.src=cover;
        const name=document.createElement('strong');name.textContent=group.name;const n=document.createElement('span');n.textContent=i18n.t('{count} photos',{count:count.toLocaleString(i18n.locale())});
        button.append(img,name,n);
        button.onclick=()=>{if(kind==='albums')picker.album=group;else picker.person=group;showHead(group.name);showPhotos();};
        pickerGrid.append(button);
      }
      status(pickerGrid.childElementCount?i18n.t('Choose a photograph to add.'):kind==='albums'?i18n.t('No albums yet.'):i18n.t('Nobody is named yet.'));
    }catch(error){if(!closed&&revision===picker.revision)status(error.message||i18n.t('Could not reach the library.'));}
  }
  function openSource(sourceName){
    picker.source=sourceName;picker.album=null;picker.person=null;showHead('');
    dialog.querySelectorAll('[data-source]').forEach(tab=>tab.setAttribute('aria-selected',String(tab.dataset.source===sourceName)));
    // A search looks through everything; picking a tab after one starts afresh.
    if(sourceName==='albums'||sourceName==='people'){picker.q='';$('[data-picker-search]').value='';showGroups(sourceName);}else showPhotos();
  }
  async function pick(item,button){
    if(busy)return;
    const already=photos.findIndex(p=>p.libraryId===item.id);
    if(already>=0){if(removeAt(already))status(i18n.t('Taken out of this creation.'));return;}
    if(photos.length>=MAX_PHOTOS){status(i18n.t('A creation supports up to 12 photos.'));return;}
    busy=true;button.disabled=true;
    try{const r=await fetch(item.view,{credentials:'same-origin'});if(!r.ok)throw new Error(i18n.t('Photo could not be loaded.'));const p=await makePhoto(await r.blob(),item.name||i18n.t('Library photo'),{...fromLibrary(item),libraryId:item.id});if(closed){p.image.close();return;}photos.push(p);changed=true;activeIndex=photos.length-1;list();status(i18n.t('Added from the library.'));}
    catch(error){if(!closed)status(error.message);}finally{busy=false;button.disabled=false;}
  }
  $('[data-library]').onclick=()=>{
    const panel=$('[data-picker]'),open=panel.hidden;panel.hidden=!open;$('[data-library]').setAttribute('aria-expanded',String(open));
    // Opened afresh each time, so what was added to the library meanwhile is there.
    if(open)openSource(picker.source);
  };
  dialog.querySelectorAll('[data-source]').forEach(tab=>{tab.onclick=()=>openSource(tab.dataset.source);});
  $('[data-picker-back]').onclick=()=>openSource(picker.source);
  $('[data-picker-more]').onclick=()=>showPhotos(true);
  let searchTimer=null;
  $('[data-picker-search]').oninput=e=>{
    clearTimeout(searchTimer);
    searchTimer=setTimeout(()=>{
      picker.q=e.target.value.trim();
      if(picker.source==='albums'&&!picker.album||picker.source==='people'&&!picker.person){picker.source='recent';dialog.querySelectorAll('[data-source]').forEach(tab=>tab.setAttribute('aria-selected',String(tab.dataset.source==='recent')));}
      showPhotos();
    },300);
  };

  // -- into the library, the way an edit goes ---------------------------------------
  $('[data-save-library]').onclick=()=>{
    stop();render();if(busy||!canSave||!sourceId)return;busy=true;$('[data-save-library]').disabled=true;status(i18n.t('Saving new library copy…'));
    canvas.toBlob(async blob=>{
      try{if(!blob)throw new Error(i18n.t('Export failed.'));const copy=await saveLibraryCopy(sourceId,blob);changed=false;status(copy.pending?i18n.t(copy.message):i18n.t('Copy saved to the library.'));onSaved?.(copy);}
      catch(error){if(!closed)status(error.message);}finally{busy=false;$('[data-save-library]').disabled=false;}
    },'image/png');
  };
  $('[data-capture]').onclick=async()=>{if(capturing||!active())return;if(versions.length>=11){status(i18n.t('Keep up to 11 versions per sheet.'));return;}capturing=true;$('[data-capture]').disabled=true;const filter=$('[data-look]').value,name=$('[data-version-name]').value||i18n.t('Version {number}',{number:versions.length+1});try{const image=await createImageBitmap(active().image);if(closed){image.close();return;}versions.push({image,filter,name});changed=true;render();status(i18n.t('Version kept in this workspace. Download the sheet before closing.'));}catch(error){if(!closed)status(error.message);}finally{capturing=false;if(!closed)$('[data-capture]').disabled=false;}};
  $('[data-version-download]').onclick=()=>{if(!active())return;const c=document.createElement('canvas');c.width=active().image.width;c.height=active().image.height;const x=c.getContext('2d');x.filter=$('[data-look]').value;x.drawImage(active().image,0,0);c.toBlob(blob=>blob&&download(blob,'ninaivu-creative-version.png'));};
  function documentHTML(){
    const title=escapeHTML($('[data-title]').value),story=escapeHTML($('[data-story]').value),current=mode();
    const seconds=Math.max(2,Math.min(30,Number($('[data-seconds]').value)||5));
    let content='';
    if(current==='restoration'||current==='compare'){
      const temp=document.createElement('canvas');temp.width=1440;temp.height=1080;const t=temp.getContext('2d');
      cover(t,current==='restoration'?source:active().image,0,0,1440,1080);const before=temp.toDataURL('image/jpeg',.92);
      cover(t,current==='restoration'?restored:(comparisonPhoto?.image||active().image),0,0,1440,1080,'none',current==='compare');
      const comparison=Number($('[data-compare]').value);
      content=`<div class="compare"><img alt="${escapeHTML(i18n.t('After'))}" src="${temp.toDataURL('image/jpeg',.92)}"><img id="before" style="clip-path:inset(0 ${100-comparison}% 0 0)" alt="${escapeHTML(i18n.t('Before'))}" src="${before}"></div><label>${escapeHTML(i18n.t('Before / after'))} <input id="comparison" type="range" min="0" max="100" value="${comparison}"></label><script>document.getElementById('comparison').oninput=e=>document.getElementById('before').style.clipPath='inset(0 '+(100-e.target.value)+'% 0 0)';<\/script>`;
    }else if(current==='album'||current==='slideshow'){
      content=photos.map((p,i)=>`<figure ${current==='slideshow'?`class="slide" ${i?'hidden':''}`:''}><img src="${p.data}" alt="${escapeHTML(p.caption)}"><figcaption>${escapeHTML(captionOf(p))}</figcaption></figure>`).join('');
      if(audio)content+=`<audio controls src="${audio}"></audio>`;
      if(current==='slideshow'){content+=`<div class="controls"><button id="previous">${escapeHTML(i18n.t('Previous'))}</button><button id="play">${escapeHTML(i18n.t('Play / pause'))}</button><button id="next">${escapeHTML(i18n.t('Next'))}</button></div><script>const slides=[...document.querySelectorAll('.slide')];let index=0,timer;function show(n){slides[index].hidden=true;index=(n+slides.length)%slides.length;slides[index].hidden=false;}function stop(){clearInterval(timer);timer=null;document.querySelector('audio')?.pause();}document.getElementById('previous').onclick=()=>show(index-1);document.getElementById('next').onclick=()=>show(index+1);document.getElementById('play').onclick=()=>{if(timer){stop();return;}timer=setInterval(()=>show(index+1),${seconds*1000});document.querySelector('audio')?.play().catch(()=>{});};document.addEventListener('visibilitychange',()=>{if(document.hidden)stop();});<\/script>`;}
    }else content=`<img alt="${title}" src="${canvas.toDataURL('image/png')}">`;
    // The page follows its reader's light or dark setting, in the same warm
    // paper and type as the app; Tamil in a Tamil face wherever the reader has one.
    const style=`:root{color-scheme:light dark;--paper:#faf7f1;--ink:#20232b;--muted:#5b6675;--line:rgba(16,21,28,.12);--card:#fff;--accent:#b07d39}@media(prefers-color-scheme:dark){:root{--paper:#141210;--ink:#efe9e0;--muted:#b1a79b;--line:rgba(255,244,230,.12);--card:#1e1b19;--accent:#e6c89b}}*{box-sizing:border-box}body{margin:0 auto;padding:40px 20px 56px;max-width:1000px;background:var(--paper);color:var(--ink);font:18px/1.6 "Inter","Tamil Sangam MN","Noto Sans Tamil",system-ui,-apple-system,"Segoe UI",sans-serif}h1{font-size:clamp(28px,5vw,44px);line-height:1.15;font-weight:650;margin:0 0 12px;text-wrap:balance}p,figcaption{white-space:pre-wrap;overflow-wrap:anywhere}body>p{color:var(--muted);margin:0 0 28px}img{display:block;max-width:100%;max-height:80vh;object-fit:contain;margin:auto;border-radius:12px}figure{margin:28px 0;padding:14px;background:var(--card);border:1px solid var(--line);border-radius:18px;box-shadow:0 18px 48px rgba(0,0,0,.08)}figcaption{margin-top:10px;color:var(--muted);font-size:15px;text-align:center}button,input,audio{font:inherit;margin:8px;max-width:95%;accent-color:var(--accent)}button{padding:8px 16px;border-radius:999px;border:1px solid var(--line);background:var(--card);color:var(--ink);cursor:pointer}.controls{display:flex;justify-content:center;flex-wrap:wrap}.compare{position:relative;border-radius:12px;overflow:hidden}.compare img{width:100%;max-height:none;border-radius:0}.compare #before{position:absolute;inset:0;clip-path:inset(0 50% 0 0)}label{display:block;text-align:center;color:var(--muted)}.slide{overflow:hidden}.slide img{animation:drift ${seconds}s ease-in-out both}@keyframes drift{from{transform:scale(1)}to{transform:scale(1.04)}}[hidden]{display:none!important}@media(prefers-reduced-motion:reduce){.slide img{animation:none}}@media print{:root{--paper:#fff;--ink:#000;--card:#fff}body{padding:0}button,input,audio,.controls{display:none}figure{break-inside:avoid;box-shadow:none}img{max-height:85vh}}`;
    return `<!doctype html><html lang="${i18n.language()}"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>${title}</title><style>${style}</style><h1>${title}</h1><p>${story}</p>${content}</html>`;
  }
  $('[data-export]').onclick=()=>{stop();render();if($('[data-export]').disabled)return;const creation=mode();if(PAGES.includes(creation))download(new Blob([documentHTML()],{type:'text/html'}),`ninaivu-${creation}.html`);else canvas.toBlob(blob=>blob&&download(blob,`ninaivu-${creation}.png`));status(i18n.t('Download prepared. Keep the file to preserve this creation.'));};
  $('[data-print]').onclick=()=>{stop();render();const win=window.open('','_blank');if(!win){status(i18n.t('Allow a popup to open the print layout.'));return;}const printable=documentHTML().replace(/<script>[\s\S]*?<\/script>/g,'').replace(/<audio[\s\S]*?<\/audio>/g,'').replace('</style>','@media print{.slide[hidden]{display:block!important}}</style>');win.document.open();win.document.write(printable);win.document.close();win.opener=null;Promise.all([...win.document.images].map(img=>img.decode().catch(()=>{}))).then(()=>{if(!win.closed){win.focus();win.print();}});};
  const now=new Date();$('[data-month]').value=`${now.getFullYear()}-${String(now.getMonth()+1).padStart(2,'0')}`;
  dialog.showModal();
  if(!editing)startFromLibrary();

  /** The photographs it was opened with, fetched the way the picker fetches one. */
  async function startFromLibrary(){
    try{
      let items=[];
      if(ids?.length)items=await Promise.all(ids.map(id=>api.asset(id).catch(()=>null)));
      else if(filters)items=(await api.assets({...filters,kinds:['picture']},{limit:MAX_PHOTOS})).items||[];
      if(closed)return;
      const pictures=items.filter(item=>item?.kind==='picture'&&item.view);
      for(const item of pictures.slice(0,MAX_PHOTOS)){
        try{const r=await fetch(item.view,{credentials:'same-origin'});if(!r.ok)continue;const p=await makePhoto(await r.blob(),item.name||i18n.t('Library photo'),{...fromLibrary(item),libraryId:item.id});if(closed){p.image.close();return;}photos.push(p);list();}
        catch{/* one photograph that will not open leaves the others */}
      }
      if(closed)return;
      if(!photos.length){$('[data-library]').click();status(i18n.t('Choose photos from the library to begin.'));return;}
      refresh();
      status(pictures.length>MAX_PHOTOS?i18n.t('A creation supports up to 12 photos.'):photos.length===1?i18n.t('1 photo ready.'):i18n.t('{count} photos ready.',{count:photos.length}));
    }catch(error){if(!closed)status(error.message||i18n.t('Could not reach the library.'));}
  }
}
