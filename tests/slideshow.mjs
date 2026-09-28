import {test} from 'node:test';
import assert from 'node:assert/strict';
import {Viewer} from '../ninaivu/static/js/viewer.js';

function viewer() {
  const v=Object.create(Viewer.prototype);
  Object.assign(v,{ids:[1,2,3],index:1,slideshow:1,slideshowDelay:4000,slideshowLoop:true,
    root:{querySelector:()=>({classList:{remove(){},add(){}},setAttribute(){}})},
    stage:{querySelector:()=>null},goTo:async function(index){this.index=index;}});
  return v;
}
test('slideshow advances, loops and respects stop at end',async()=>{
  const v=viewer();await v.advanceSlideshow();assert.equal(v.index,2);
  await v.advanceSlideshow();assert.equal(v.index,0);
  v.index=2;v.slideshowLoop=false;await v.advanceSlideshow();assert.equal(v.index,2);assert.equal(v.slideshow,null);
});
test('loading the next item cannot overlap another automatic advance',async()=>{
  const v=viewer();let complete,calls=0;v.goTo=()=>{calls++;return new Promise(r=>complete=r);};
  const pending=v.advanceSlideshow();await v.advanceSlideshow();assert.equal(calls,1);complete();await pending;
});
test('timer honors media playback, tab visibility and configured delay',async()=>{
  const oldInterval=globalThis.setInterval,oldClear=globalThis.clearInterval,oldDocument=globalThis.document;
  let tick,delay,calls=0;globalThis.setInterval=(fn,ms)=>{tick=fn;delay=ms;return 1;};globalThis.clearInterval=()=>{};globalThis.document={hidden:false};
  try {
    const v=viewer();v.slideshowDelay=8000;v.advanceSlideshow=()=>calls++;
    v.stage.querySelector=()=>({ended:false,error:null});v.scheduleSlideshow();tick();assert.equal(calls,0);assert.equal(delay,8000);
    v.stage.querySelector=()=>({ended:true,error:null});tick();assert.equal(calls,1);
    document.hidden=true;tick();assert.equal(calls,1);
    document.hidden=false;v.stage.querySelector=()=>null;tick();assert.equal(calls,2);
    v.stage.querySelector=selector=>selector==='.proxy-bar'?{}:null;tick();assert.equal(calls,2);
  } finally {globalThis.setInterval=oldInterval;globalThis.clearInterval=oldClear;globalThis.document=oldDocument;}
});
