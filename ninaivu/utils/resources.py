"""Bounded resource budgets selected when the Ninaivu server starts."""
import math
import os

MODES = ('standard', 'performance', 'power-saving')

#: What the Tuning page decided for this machine (server/tuning.py), once the
#: server has started; the resource mode's numbers until then.
_TUNED = {}


def tune(**values):
    """Remember the tuned numbers the parts below ask for."""
    _TUNED.update({k: int(v) for k, v in values.items() if v})


def compute_threads():
    """Threads the image model, ffmpeg and the editing models may use."""
    return max(1, int(_TUNED.get('compute_threads') or budget()['compute_threads']))


#: The most a helper pool (face reads, video moments, audio pictures, the
#: image model's decoding) is given, however large the machine.
MAX_HELPERS = 8


def helpers(workers, cap=MAX_HELPERS):
    """Threads for a pass's helper pool, from the scan workers the tuning
    chose: as many as the workers up to four, as before, and half of them
    beyond that, up to *cap*. Four was the limit whatever the machine, so an
    eighteen-core Mac read four videos at a time with fourteen cores idle."""
    try:
        workers = int(workers or 1)
    except (TypeError, ValueError):
        workers = 1
    return max(1, min(cap, max(min(4, workers), workers // 2)))


def budget(mode=None, cpus=None):
    mode = mode or os.environ.get('NINAIVU_RESOURCE_MODE', 'standard')
    if mode not in MODES:
        mode = 'standard'
    count = max(1, cpus or os.cpu_count() or 4)
    if mode == 'performance':
        # The Peak plan's share (server/tuning.py): every core, 18 of 18.
        workers, compute, requests = count, count, 16
    elif mode == 'power-saving':
        workers, compute, requests = 1, min(2,count), 4
    else:
        # Half the cores (Everyday computer in server/tuning.py): 9 of 18.
        # It stopped at eight, whatever the machine.
        half = max(1, math.floor(count * 0.5))
        workers, compute, requests = half, half, 8
    return {'mode':mode, 'workers':workers, 'compute_threads':compute, 'server_threads':requests,
            'model_keep_alive':0}


def environment(mode):
    values = budget(mode)
    return {'NINAIVU_RESOURCE_MODE': values['mode'], 'NINAIVU_WORKERS': str(values['workers']),
            'NINAIVU_SERVER_THREADS': str(values['server_threads']),
            'OMP_NUM_THREADS': str(values['compute_threads']),
            'MKL_NUM_THREADS': str(values['compute_threads']),
            'OPENCV_FOR_THREADS_NUM': str(values['compute_threads']),
            'OPENBLAS_NUM_THREADS': str(values['compute_threads'])}
