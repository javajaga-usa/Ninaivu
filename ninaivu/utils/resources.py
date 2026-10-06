"""Bounded resource budgets selected when the Ninaivu server starts."""
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


def budget(mode=None, cpus=None):
    mode = mode or os.environ.get('NINAIVU_RESOURCE_MODE', 'standard')
    if mode not in MODES:
        mode = 'standard'
    count = max(1, cpus or os.cpu_count() or 4)
    if mode == 'performance':
        workers, compute, requests = min(16,count), min(16,count), 16
    elif mode == 'power-saving':
        workers, compute, requests = 1, min(2,count), 4
    else:
        workers, compute, requests = min(8,max(1,count//2)), min(8,max(1,count//2)), 8
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
