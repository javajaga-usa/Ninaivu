# Third-party notices

Ninaivu itself is MIT licensed (see `LICENSE`). This file lists what it
bundles or downloads that was made by others, and under what terms.

## Bundled with Ninaivu

### Leaflet 1.9.4

`ninaivu/static/js/leaflet.js` — the map in the family app.
<https://leafletjs.com>

```
BSD 2-Clause License

Copyright (c) 2010-2023, Vladimir Agafonkin
Copyright (c) 2010-2011, CloudMade
All rights reserved.

Redistribution and use in source and binary forms, with or without modification, are
permitted provided that the following conditions are met:

   1. Redistributions of source code must retain the above copyright notice, this list of
      conditions and the following disclaimer.

   2. Redistributions in binary form must reproduce the above copyright notice, this list
      of conditions and the following disclaimer in the documentation and/or other materials
      provided with the distribution.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND ANY
EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED WARRANTIES OF
MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE
COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL,
SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT
OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION)
HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR
TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS
SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
```

### World outline

`ninaivu/static/data/world.geojson` — the map's built-in outline of the
world. Natural Earth, `ne_110m_admin_0_countries`, generalised. Public
domain; no attribution required. <https://www.naturalearthdata.com>

### Noto Sans Tamil (Windows build only)

`ninaivu/static/fonts/NotoSansTamil.ttf` — the Tamil font of the Windows
installer, fetched by `installers/windows/build.ps1` from
[google/fonts](https://github.com/google/fonts/tree/main/ofl/notosanstamil)
at a pinned commit and checked by SHA-256. Copyright 2022 The Noto Project
Authors. SIL Open Font License 1.1; the licence travels beside the font as
`NotoSansTamil-OFL.txt`. Other builds use the device's own Tamil font, and
Apple devices prefer their own Tamil Sangam MN everywhere.

### Noto Serif, Noto Sans and their Tamil faces (the PDF guides only)

The guide PDFs (`docs/Ninaivu-guide.pdf`, `docs/Ninaivu-guide-ta.pdf`,
`docs/Ninaivu-admin-guide.pdf`) are printed from the Debian/Ubuntu package
`fonts-noto-core`, and the subset of each face they use is embedded in the PDF.
The fonts are not part of any installer or of this repository. Copyright The
Noto Project Authors. SIL Open Font License 1.1, which allows embedding in documents.

## Downloaded when a feature is turned on

None of these ship with Ninaivu. Each is fetched from its source when an
administrator turns its feature on, and its licence is shown next to it on
the **AI models** page.

| Model | Used for | Licence | Source |
| --- | --- | --- | --- |
| YuNet and SFace | Finding and grouping faces | Apache-2.0 | OpenCV Zoo |
| Deep image orientation detection v2 | Which way up a photograph goes | MIT | duartebarbosadev/deep-image-orientation-detection |
| LaMa | Object removal | Apache-2.0 | Carve/LaMa-ONNX |
| Real-ESRGAN | Upscaling ×4 | BSD-3-Clause | facefusion/models-3.0.0 (conversion states no licence of its own) |
| GFPGAN 1.4 | Restoring faces | Apache-2.0 | facefusion/models-3.0.0 (conversion states no licence of its own) |
| SigLIP 2, ViT-B/16 | Search by description | Apache-2.0 | timm/ViT-B-16-SigLIP2 |
| OpenCLIP ViT-B-32 (LAION-2B) | Search by description (default weights) | MIT | laion2b_s34b_b79k via OpenCLIP |
| MagicBrush | Generative editing (Creative Studio) | CreativeML Open RAIL-M | vinesmsuic/magicbrush-jul7 |
| RMBG-1.4 | Background removal | BRIA community licence — **non-commercial use only** | briaai/RMBG-1.4 |

The list of places for naming locations is GeoNames `cities1000`, licensed
CC BY 4.0 (<https://www.geonames.org>), downloaded once when *Name the
places* is switched on.

## Python packages

The packages in `requirements/*.txt` are installed by pip from PyPI (and
PyTorch from its own index), each under its own licence, and are not
redistributed with Ninaivu's source. The Windows and macOS installers bundle
them as wheels; their licence files travel inside each wheel.
