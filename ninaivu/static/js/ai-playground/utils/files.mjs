export function validateFile(file) {
  if (!file || !['image/jpeg', 'image/png', 'image/webp'].includes(file.type)) throw new Error('Choose a JPEG, PNG, or WebP image.');
  if (!file.size || file.size > 30 * 1024 * 1024) throw new Error('Choose an image smaller than 30 MB.');
}
export async function decode(file) {
  validateFile(file);
  const bitmap = await createImageBitmap(file);
  if (bitmap.width * bitmap.height > 24000000 || bitmap.width > 12000 || bitmap.height > 12000) {
    bitmap.close();
    throw new Error('Choose an image up to 24 megapixels and 12,000 pixels per side. Exports retain the accepted image’s resolution.');
  }
  return bitmap;
}
