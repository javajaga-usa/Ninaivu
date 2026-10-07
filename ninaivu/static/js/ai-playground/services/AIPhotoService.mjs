import {analyze, suggestions} from '../models/adjustments.mjs';
import {interpret, planRequest, checkRequest} from '../safety/commands.mjs';
import {validate} from '../models/adjustments.mjs';
import {render} from './processor.mjs';
import * as i18n from '../../i18n.js';

//: Background segmentation and object removal cap at 1600px on the long side
//: to match the server's own limit (see ninaivu/media/segmentation.py, inpaint.py).
const BACKGROUND_MAX_SIDE=1600;

/** The Playground's status line for an AI server job. */
export function describeServerJob(state, local = false) {
  if (state?.state === 'queued') {
    return state.ahead ? i18n.t('Waiting on the AI server — {count} ahead of you…', {count: state.ahead}) : i18n.t('Waiting on the AI server…');
  }
  if (state?.state === 'running') {
    const percent = state.max ? Math.round((100 * state.value) / state.max) : null;
    if (local) return percent === null ? i18n.t('Working on your Ninaivu server…') : i18n.t('Working on your Ninaivu server… {percent}%', {percent});
    return percent === null ? i18n.t('Working on the AI server…') : i18n.t('Working on the AI server… {percent}%', {percent});
  }
  return local ? i18n.t('Starting on your Ninaivu server…') : i18n.t('Sending to the AI server…');
}

const sleep = (ms, signal) => new Promise((resolve, reject) => {
  const timer = setTimeout(resolve, ms);
  signal?.addEventListener('abort', () => { clearTimeout(timer); reject(new DOMException('Aborted', 'AbortError')); }, {once: true});
});

async function errorFrom(response, fallback) {
  try { return (await response.json()).error || fallback; } catch { return fallback; }
}

function blobToBase64(blob) {
  return new Promise((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve(reader.result.split(',')[1]);reader.onerror=reject;reader.readAsDataURL(blob);});
}

export class AIPhotoService {
  /** ``mediaId`` names the library item being edited. Every request that can
   *  leave this machine carries it, so the server can keep hidden items away
   *  from Gemini and the AI server (ai_playground_api._hidden). */
  constructor(mediaId=null) { this.mediaId = mediaId; }
  _tag(body) { if (this.mediaId != null) body.media_id = this.mediaId; return body; }
  analyzeImage(bitmap) {
    const scale=Math.min(1,256/Math.max(bitmap.width,bitmap.height));
    const c=document.createElement('canvas'); c.width=Math.max(1,Math.round(bitmap.width*scale)); c.height=Math.max(1,Math.round(bitmap.height*scale));
    const ctx=c.getContext('2d',{willReadFrequently:true}); ctx.drawImage(bitmap,0,0,c.width,c.height);
    return analyze(ctx.getImageData(0,0,c.width,c.height).data,bitmap.width,bitmap.height);
  }
  getSuggestions(analysis) { return suggestions(analysis); }
  executeNaturalLanguageEdit(text) { return interpret(text); }
  async planEdit(text,current,analysis,provider,signal,image=null) {
    checkRequest(text);
    if(provider==='builtin')return planRequest(text,current,analysis);
    const body = this._tag({prompt:text,current,provider});
    if(image) body.image = image;
    const response=await fetch('/api/ai-playground/plan',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body),signal});
    const data=await response.json();
    if(!response.ok)throw new Error(data.error||i18n.t('AI planning failed.'));
    validate({...current,...data.patch});
    if(!data.patch||typeof data.patch!=='object'||Array.isArray(data.patch))throw new Error(i18n.t('Invalid plan.'));
    return data;
  }
  /** Analyze photo using Google Gemini multimodal vision. */
  async geminiAnalyze(bitmap,current,signal) {
    const blob=await this.applyAdjustments(bitmap,current,{maxSide:1024,type:'image/jpeg'});
    const image=await blobToBase64(blob);
    const response=await fetch('/api/ai-playground/gemini/analyze',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(this._tag({image})),signal});
    if(!response.ok){const data=await response.json();throw new Error(data.error||i18n.t('Gemini analysis failed.'));}
    return response.json();
  }
  /** Run a job on the AI server in the background, reporting its state until the image is ready. */
  async runServerJob(body,signal,onStatus=()=>{}) {
    const started=await fetch('/api/ai-playground/server-jobs',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(this._tag({...body})),signal});
    if(!started.ok)throw new Error(await errorFrom(started,i18n.t('The AI server could not start this edit.')));
    const {id,status}=await started.json();
    onStatus(status);
    for(;;){
      await sleep(1000,signal);
      const polled=await fetch(`/api/ai-playground/server-jobs/${encodeURIComponent(id)}`,{signal});
      if(!polled.ok)throw new Error(await errorFrom(polled,i18n.t('The AI server job was lost. Try again.')));
      const state=await polled.json();
      if(state.state==='error')throw new Error(state.error||i18n.t('The AI server could not finish this edit.'));
      if(state.state==='done'){
        const result=await fetch(`/api/ai-playground/server-jobs/${encodeURIComponent(id)}/result`,{signal});
        if(!result.ok)throw new Error(await errorFrom(result,i18n.t('The finished image could not be collected.')));
        return result.blob();
      }
      onStatus(state);
    }
  }
  /** Upscale, restore or colourise the current edit on the AI server. */
  async serverTool(kind,bitmap,current,signal,maxSide,onStatus) {
    const blob=await this.applyAdjustments(bitmap,current,{maxSide,type:'image/png'});
    return this.runServerJob({kind,image:await blobToBase64(blob)},signal,onStatus);
  }
  async generateEdit(text,bitmap,current,signal,maxSide=512,options={},{serverJob=false,onStatus,provider}={}) {
    checkRequest(text);
    const blob=await this.applyAdjustments(bitmap,current,{maxSide,type:'image/png'});
    const image=await blobToBase64(blob);
    if(serverJob)return this.runServerJob({kind:'edit',prompt:text,image,options},signal,onStatus);
    const body=this._tag({prompt:text,image,options});
    if(provider) body.provider=provider;
    const response=await fetch('/api/ai-playground/generate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body),signal});
    if(!response.ok){const data=await response.json();throw new Error(data.error||i18n.t('Generative editing failed.'));}
    return response.blob();
  }
  enhanceImage(bitmap, a) { return this.applyAdjustments(bitmap,a); }
  /** Send the current edit (with adjustments/crop baked in) to the local segmentation model
   *  and get back a matching subject bitmap plus a foreground mask. Callers composite locally
   *  with compositeCutout/compositeBlur so a blur-strength slider needs no further network call. */
  async computeBackground(bitmap,current,signal) {
    const subjectBlob=await this.applyAdjustments(bitmap,current,{maxSide:BACKGROUND_MAX_SIDE,type:'image/png'});
    const image=await blobToBase64(subjectBlob);
    const response=await fetch('/api/ai-playground/segment',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({image}),signal});
    if(!response.ok){const data=await response.json();throw new Error(data.error||i18n.t('Background segmentation failed.'));}
    const maskBlob=await response.blob();
    const [subject,mask]=await Promise.all([createImageBitmap(subjectBlob),createImageBitmap(maskBlob)]);
    return {subject,mask};
  }
  async compositeCutout(subject,mask) {
    const canvas=typeof OffscreenCanvas!=='undefined'?new OffscreenCanvas(subject.width,subject.height):Object.assign(document.createElement('canvas'),{width:subject.width,height:subject.height});
    const ctx=canvas.getContext('2d');
    ctx.drawImage(subject,0,0);
    ctx.globalCompositeOperation='destination-in';
    ctx.drawImage(mask,0,0,subject.width,subject.height);
    return canvas.convertToBlob?canvas.convertToBlob({type:'image/png'}):new Promise((resolve,reject)=>canvas.toBlob(b=>b?resolve(b):reject(new Error(i18n.t('Export failed.'))),'image/png'));
  }
  async compositeBlur(subject,mask,amount=16) {
    const w=subject.width,h=subject.height;
    const fg=typeof OffscreenCanvas!=='undefined'?new OffscreenCanvas(w,h):Object.assign(document.createElement('canvas'),{width:w,height:h});
    const fctx=fg.getContext('2d');
    fctx.drawImage(subject,0,0);fctx.globalCompositeOperation='destination-in';fctx.drawImage(mask,0,0,w,h);
    const canvas=typeof OffscreenCanvas!=='undefined'?new OffscreenCanvas(w,h):Object.assign(document.createElement('canvas'),{width:w,height:h});
    const ctx=canvas.getContext('2d');
    ctx.filter=`blur(${Math.max(0,amount)}px)`;ctx.drawImage(subject,0,0);ctx.filter='none';
    ctx.drawImage(fg,0,0);
    return canvas.convertToBlob?canvas.convertToBlob({type:'image/png'}):new Promise((resolve,reject)=>canvas.toBlob(b=>b?resolve(b):reject(new Error(i18n.t('Export failed.'))),'image/png'));
  }
  /** Colour pop: the subject keeps its colour and everything behind it goes to black and white. */
  async compositeColourPop(subject,mask) {
    const w=subject.width,h=subject.height;
    const fg=typeof OffscreenCanvas!=='undefined'?new OffscreenCanvas(w,h):Object.assign(document.createElement('canvas'),{width:w,height:h});
    const fctx=fg.getContext('2d');
    fctx.drawImage(subject,0,0);fctx.globalCompositeOperation='destination-in';fctx.drawImage(mask,0,0,w,h);
    const canvas=typeof OffscreenCanvas!=='undefined'?new OffscreenCanvas(w,h):Object.assign(document.createElement('canvas'),{width:w,height:h});
    const ctx=canvas.getContext('2d');
    ctx.filter='grayscale(1)';ctx.drawImage(subject,0,0);ctx.filter='none';
    ctx.drawImage(fg,0,0);
    return canvas.convertToBlob?canvas.convertToBlob({type:'image/png'}):new Promise((resolve,reject)=>canvas.toBlob(b=>b?resolve(b):reject(new Error(i18n.t('Export failed.'))),'image/png'));
  }
  /** The painted area redrawn to a request on the AI server: hair where there is none, say. */
  async inpaint(subject,maskBlob,prompt,signal,{onStatus}={}) {
    checkRequest(prompt);
    const canvas=typeof OffscreenCanvas!=='undefined'?new OffscreenCanvas(subject.width,subject.height):Object.assign(document.createElement('canvas'),{width:subject.width,height:subject.height});
    canvas.getContext('2d').drawImage(subject,0,0);
    const subjectBlob=await(canvas.convertToBlob?canvas.convertToBlob({type:'image/png'}):new Promise((resolve,reject)=>canvas.toBlob(b=>b?resolve(b):reject(new Error(i18n.t('Export failed.'))),'image/png')));
    const [image,mask]=await Promise.all([blobToBase64(subjectBlob),blobToBase64(maskBlob)]);
    return this.runServerJob({kind:'inpaint',image,mask,prompt,options:{}},signal,onStatus);
  }
  /** subject is the already-rendered ImageBitmap the caller painted on (see openRemoveObject);
   *  maskBlob must be scaled to the same dimensions before calling. */
  async removeObject(subject,maskBlob,signal,{serverJob=false,onStatus}={}) {
    const canvas=typeof OffscreenCanvas!=='undefined'?new OffscreenCanvas(subject.width,subject.height):Object.assign(document.createElement('canvas'),{width:subject.width,height:subject.height});
    canvas.getContext('2d').drawImage(subject,0,0);
    const subjectBlob=await(canvas.convertToBlob?canvas.convertToBlob({type:'image/png'}):new Promise((resolve,reject)=>canvas.toBlob(b=>b?resolve(b):reject(new Error(i18n.t('Export failed.'))),'image/png')));
    const [image,mask]=await Promise.all([blobToBase64(subjectBlob),blobToBase64(maskBlob)]);
    if(serverJob)return this.runServerJob({kind:'remove',image,mask},signal,onStatus);
    const response=await fetch('/api/ai-playground/inpaint',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({image,mask}),signal});
    if(!response.ok){const data=await response.json();throw new Error(data.error||i18n.t('Object removal failed.'));}
    return response.blob();
  }
  async applyAdjustments(bitmap, adjustments, {maxSide=1400,type='image/png'}={}) {
    if (typeof Worker !== 'undefined' && typeof OffscreenCanvas !== 'undefined') {
      const copy=await createImageBitmap(bitmap);
      try {
        return await new Promise((resolve,reject)=>{
          const worker=new Worker(new URL('./worker.mjs',import.meta.url),{type:'module'});
          const timer=setTimeout(()=>{worker.terminate();reject(new Error(i18n.t('Processing timed out. Try a smaller image.')));},120000);
          const finish=()=>{clearTimeout(timer);worker.terminate();};
          worker.onmessage=({data})=>{finish();data.error?reject(new Error(data.error)):resolve(data.blob);};
          worker.onerror=()=>{finish();reject(new Error('Worker unavailable'));};
          worker.postMessage({bitmap:copy,adjustments,maxSide,type},[copy]);
        });
      } catch { copy.close(); /* Cooperative CPU fallback, without network calls. */ }
    }
    const canvas=await render(bitmap,adjustments,maxSide);
    if(canvas.convertToBlob) return canvas.convertToBlob({type,quality:.94});
    return new Promise((resolve,reject)=>canvas.toBlob(b=>b?resolve(b):reject(new Error(i18n.t('Export failed.'))),type,.94));
  }
}
