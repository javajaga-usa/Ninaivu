"""Exercise inference contracts without model downloads or personal images."""
import io
import sys
from types import SimpleNamespace
from contextlib import nullcontext

import pytest
from PIL import Image

from ninaivu.media import generative_editing as editing


@pytest.mark.parametrize('options', [[], {'unknown': 1}, {'seed': True}, {'seed': -1},
                                     {'seed': 2147483648}, {'quality': []}, {'fidelity': 'extreme'},
                                     {'negative_prompt': 123}, {'negative_prompt': 'x' * 501}])
def test_reject_options(options):
    with pytest.raises(ValueError):
        editing.generation_options(options)


def test_quality_bounded_by_supported_steps(monkeypatch):
    monkeypatch.setenv('NINAIVU_IMAGE_EDIT_STEPS', '30')
    assert editing.generation_options({'quality': 'detailed'})['num_inference_steps'] == 30
    monkeypatch.setenv('NINAIVU_IMAGE_EDIT_STEPS', '4')
    assert editing.generation_options({'quality': 'draft'})['num_inference_steps'] == 4
    assert editing.generation_options()['image_guidance_scale'] == 2.0


@pytest.mark.parametrize('unsafe_input,unsafe_output', [(False, False), (True, False), (False, True)])
def test_local_inference_controls_geometry_and_safety(tmp_path, monkeypatch, unsafe_input, unsafe_output):
    (tmp_path / 'model_index.json').write_text('{}')
    monkeypatch.setenv('NINAIVU_IMAGE_EDIT_MODEL', str(tmp_path))
    calls = {}

    class Pipeline:
        safety_checker = feature_extractor = object()
        scheduler = SimpleNamespace(config={})
        image_processor = SimpleNamespace(pil_to_numpy=lambda image: image)

        @classmethod
        def from_pretrained(cls, path, **kwargs):
            assert kwargs['local_files_only'] and kwargs['use_safetensors']
            return cls()

        def enable_attention_slicing(self):
            pass

        def to(self, device):
            assert device == 'cpu'

        def run_safety_checker(self, *args):
            return None, [unsafe_input]

        def __call__(self, **kwargs):
            calls.update(kwargs)
            return SimpleNamespace(images=[kwargs['image']], nsfw_content_detected=[unsafe_output])

    monkeypatch.setitem(sys.modules, 'diffusers', SimpleNamespace(
        StableDiffusionInstructPix2PixPipeline=Pipeline,
        EulerAncestralDiscreteScheduler=SimpleNamespace(from_config=lambda _: None)))
    monkeypatch.setitem(sys.modules, 'torch', SimpleNamespace(
        set_num_threads=lambda _: None, float32='float32', device=lambda value: value,
        inference_mode=nullcontext, Generator=lambda _: SimpleNamespace(manual_seed=lambda seed: seed)))
    source = io.BytesIO()
    Image.new('RGB', (101, 67), 'blue').save(source, 'PNG')
    options = {'seed': 123, 'quality': 'detailed', 'fidelity': 'creative', 'negative_prompt': ' blur '}
    if unsafe_input or unsafe_output:
        with pytest.raises(ValueError):
            editing.generate('Make this a painting', source.getvalue(), options)
        if unsafe_input:
            assert not calls
        return
    result = editing.generate('Make this a painting', source.getvalue(), options)
    assert calls['image'].size == (104, 72)
    assert calls['generator'] == 123
    assert calls['image_guidance_scale'] == 1.1
    assert calls['negative_prompt'] == 'blur'
    assert calls['num_inference_steps'] >= 20
    with Image.open(io.BytesIO(result)) as image:
        assert image.size == (101, 67)
        assert image.getpixel((100, 66)) == (0, 0, 255)
        assert 'seed=123' in image.info['Ninaivu generation']


def test_uses_the_graphics_card_in_half_precision_when_it_really_works(tmp_path, monkeypatch):
    """A card that can run a kernel gets the work, in float16; a broken one does not.

    ``torch.cuda.is_available()`` is not enough on its own: a ROCm build carrying
    no kernels for the installed card answers yes and then fails on the first real
    operation. The probe in ``ninaivu.ai.usable_device`` is what decides, so this
    drives both answers rather than whatever card the test machine happens to have.
    """
    (tmp_path / 'model_index.json').write_text('{}')
    monkeypatch.setenv('NINAIVU_IMAGE_EDIT_MODEL', str(tmp_path))
    seen = {}

    class Probe:
        def __matmul__(self, other):
            return self

        def sum(self):
            return 1.0

    class Pipeline:
        safety_checker = feature_extractor = object()
        scheduler = SimpleNamespace(config={})
        image_processor = SimpleNamespace(pil_to_numpy=lambda image: image)

        @classmethod
        def from_pretrained(cls, path, **kwargs):
            seen['dtype'] = kwargs['dtype']
            return cls()

        def enable_attention_slicing(self):
            pass

        def to(self, device):
            seen['device'] = device

        def run_safety_checker(self, pixels, device, dtype):
            seen['checker'] = (device, dtype)
            return None, [False]

        def __call__(self, **kwargs):
            return SimpleNamespace(images=[kwargs['image']], nsfw_content_detected=[False])

    def run(kernels_work):
        seen.clear()
        monkeypatch.setitem(sys.modules, 'diffusers', SimpleNamespace(
            StableDiffusionInstructPix2PixPipeline=Pipeline,
            EulerAncestralDiscreteScheduler=SimpleNamespace(from_config=lambda _: None)))

        def ones(*shape, **kwargs):
            if not kernels_work:
                raise RuntimeError('device kernel image is invalid')
            return Probe()

        monkeypatch.setitem(sys.modules, 'torch', SimpleNamespace(
            set_num_threads=lambda _: None, float32='float32', float16='float16',
            device=lambda value: value, inference_mode=nullcontext, ones=ones,
            cuda=SimpleNamespace(is_available=lambda: True),
            Generator=lambda _: SimpleNamespace(manual_seed=lambda seed: seed)))
        source = io.BytesIO()
        Image.new('RGB', (64, 64), 'blue').save(source, 'PNG')
        editing.generate('Make this a painting', source.getvalue(), {'seed': 1})

    run(kernels_work=True)
    assert seen['device'] == 'cuda'
    assert seen['dtype'] == 'float16'
    assert seen['checker'] == ('cuda', 'float16')

    run(kernels_work=False)
    assert seen['device'] == 'cpu'
    assert seen['dtype'] == 'float32'
    assert seen['checker'] == ('cpu', 'float32')
