import {render} from './processor.mjs';
self.onmessage = async ({data}) => {
  try {
    const canvas = await render(data.bitmap, data.adjustments, data.maxSide);
    const blob = await canvas.convertToBlob({type:data.type, quality:.94});
    self.postMessage({blob});
  } catch(error) { self.postMessage({error:error.message}); }
  finally { data.bitmap.close(); }
};
