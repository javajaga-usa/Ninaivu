//: Loaded by Node tests, so it names its English with a
//: local key() rather than importing i18n.js; the component showing the text
//: translates it with i18n.t() (see ../utils/messages.mjs).
const i18n = {key: (s) => s};

export function validateFile(file) {
  if (!file || !['image/jpeg', 'image/png', 'image/webp'].includes(file.type)) throw new Error(i18n.key('Choose a JPEG, PNG, or WebP image.'));
  if (!file.size || file.size > 30 * 1024 * 1024) throw new Error(i18n.key('Choose an image smaller than 30 MB.'));
}
export async function decode(file) {
  validateFile(file);
  const bitmap = await createImageBitmap(file);
  if (bitmap.width * bitmap.height > 24000000 || bitmap.width > 12000 || bitmap.height > 12000) {
    bitmap.close();
    throw new Error(i18n.key('Choose an image up to 24 megapixels and 12,000 pixels per side. Exports retain the accepted image’s resolution.'));
  }
  return bitmap;
}
