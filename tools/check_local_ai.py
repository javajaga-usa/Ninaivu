"""Run real local model smoke checks on a synthetic image, never household media."""
import io
import json
import sys
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PIL import Image, ImageDraw
from ninaivu.media.ai_editing import plan
from ninaivu.media.generative_editing import generate
from ninaivu.media import inpaint, segmentation

root = Path(__file__).resolve().parents[1] / '.ai-models'
started = time.monotonic()
print(json.dumps(plan('Brighten the shadows, soften highlights, keep saturation at 12, and crop to a 4:5 portrait.', {'saturation':12,'shadows':0,'highlights':0,'crop':'original'})), flush=True)
print('Language seconds:', round(time.monotonic()-started,1), flush=True)
image = Image.new('RGB', (256,256), '#84b8de')
draw = ImageDraw.Draw(image)
draw.rectangle((0,150,255,255), fill='#507a3b')
draw.rectangle((70,110,185,200), fill='#f4deba')
draw.polygon([(55,110),(128,50),(198,110)], fill='#994c35')
draw.rectangle((110,145,145,200), fill='#504334')
source = io.BytesIO();image.save(source,'PNG')
started = time.monotonic()
result = generate('Turn this house scene into a watercolor painting', source.getvalue())
with Image.open(io.BytesIO(result)) as output:
    assert output.size == (256,256)
    assert output.convert('RGB').tobytes() != image.tobytes()
    output.save(root / 'smoke-generated.png')
image.save(root / 'smoke-source.png')
print('Image seconds:', round(time.monotonic()-started,1), 'bytes:', len(result), flush=True)

if segmentation.available():
    started = time.monotonic()
    mask_bytes = segmentation.mask(source.getvalue())
    with Image.open(io.BytesIO(mask_bytes)) as mask_image:
        assert mask_image.size == (256, 256)
        assert mask_image.mode == 'L'
    print('Segmentation seconds:', round(time.monotonic()-started,1), 'bytes:', len(mask_bytes), flush=True)
else:
    print('Segmentation model not installed; skipped.', flush=True)

if inpaint.available():
    started = time.monotonic()
    paint_mask = Image.new('L', (256,256), 0)
    ImageDraw.Draw(paint_mask).rectangle((100,80,160,140), fill=255)
    mask_buffer = io.BytesIO();paint_mask.save(mask_buffer,'PNG')
    removed = inpaint.remove(source.getvalue(), mask_buffer.getvalue())
    with Image.open(io.BytesIO(removed)) as output:
        assert output.size == (256,256)
    print('Inpaint seconds:', round(time.monotonic()-started,1), 'bytes:', len(removed), flush=True)
else:
    print('opencv not installed; object-removal check skipped.', flush=True)
