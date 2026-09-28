import test from 'node:test';
import assert from 'node:assert/strict';
import {recolorPixels,isClothingColorRequest} from '../ninaivu/static/js/ai-playground/services/recolor.mjs';
test('recolor changes only selected pixels and preserves alpha, texture and source',()=>{
 const source=new Uint8ClampedArray([180,100,90,255,60,50,40,128,30,20,10,255]);
 const mask=new Uint8ClampedArray([0,0,0,255,0,0,0,255,0,0,0,0]);
 const result=recolorPixels(source,mask,[0,0,255]);
 assert.deepEqual([...result],[0,0,180,255,0,0,60,128,30,20,10,255]);
 assert.equal(source[0],180);assert.deepEqual(recolorPixels(source,mask,[255,0,0],0),source);
});
test('clothing color requests route to targeted selection, ordinary edits do not',()=>{
 for(const text of ['Dress color change','Make her jacket blue','recolor the shirt'])assert.ok(isClothingColorRequest(text));
 for(const text of ['Make the sky blue','brighten the dress','warm the photo'])assert.ok(!isClothingColorRequest(text));
});
