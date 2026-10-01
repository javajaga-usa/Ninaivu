// Deterministic color replacement: selection coverage is the only affected area.
export function recolorPixels(source, mask, color, strength=1) {
  const result=new Uint8ClampedArray(source);
  const target=color.map(v=>v/255), peak=Math.max(...target);
  for(let i=0;i<source.length;i+=4){
    const amount=mask[i+3]/255*Math.max(0,Math.min(1,strength));
    if(!amount)continue;
    // Retain brightness variation (folds and texture), replacing chroma.
    const light=Math.max(source[i],source[i+1],source[i+2])/255;
    for(let c=0;c<3;c++){
      const value=peak===0?light*.15:light*target[c]/peak;
      result[i+c]=source[i+c]*(1-amount)+value*255*amount;
    }
  }
  return result;
}

export const isClothingColorRequest=text=>/\b(dress|shirt|t-shirt|jacket|clothes|clothing|skirt|trousers|pants|coat|blouse|sweater|sari|saree|salwar|kurta|kurti|dhoti|veshti|lehenga|lungi|dupatta|scarf|hoodie|suit|uniform|top)\b/i.test(text)&&/\b(colou?r|recolou?r|red|blue|green|pink|purple|yellow|black|white|orange|teal|maroon|gold|silver|grey|gray|brown|violet|turquoise|cream|beige)\b/i.test(text);
