from types import SimpleNamespace
from tools import cleanup_workspace as cleanup


def test_cleanup_preserves_tracked_files_and_model_caches(tmp_path,monkeypatch):
    for folder in ('ninaivu/__pycache__','tests/__pycache__','.ai-models/__pycache__'):
        path=tmp_path/folder;path.mkdir(parents=True);(path/'keep.pyc').write_bytes(b'example')
    monkeypatch.setattr(cleanup.subprocess,'run',lambda *a,**k:SimpleNamespace(stdout=b'ninaivu/__pycache__/keep.pyc\0'))
    assert cleanup.clean(tmp_path,apply=False)==1
    assert (tmp_path/'tests/__pycache__').is_dir()
    cleanup.clean(tmp_path,apply=True)
    assert not (tmp_path/'tests/__pycache__').exists()
    assert (tmp_path/'ninaivu/__pycache__/keep.pyc').exists()
    assert (tmp_path/'.ai-models/__pycache__/keep.pyc').exists()


def test_cleanup_rejects_external_targets(tmp_path):
    assert not cleanup.inside(tmp_path,tmp_path)
    assert not cleanup.inside(tmp_path,tmp_path/'..'/'outside')
    assert cleanup.inside(tmp_path,tmp_path/'__pycache__')


def test_cleanup_fails_closed_without_git_index(tmp_path,monkeypatch):
    import subprocess
    import pytest
    path=tmp_path/'__pycache__';path.mkdir()
    def unavailable(*a,**k):raise subprocess.CalledProcessError(1,'git')
    monkeypatch.setattr(cleanup.subprocess,'run',unavailable)
    with pytest.raises(subprocess.CalledProcessError):cleanup.clean(tmp_path,True)
    assert path.is_dir()


def test_cleanup_supports_build_artifacts(tmp_path, monkeypatch):
    for folder in ('build', 'dist', 'ninaivu.egg-info'):
        path = tmp_path / folder
        path.mkdir(parents=True)
        (path / 'artifact.tmp').write_bytes(b'output')
    monkeypatch.setattr(cleanup.subprocess, 'run', lambda *a, **k: SimpleNamespace(stdout=b''))
    assert cleanup.clean(tmp_path, apply=False, include_build=False) == 0
    assert cleanup.clean(tmp_path, apply=False, include_build=True) == 3
    cleanup.clean(tmp_path, apply=True, include_build=True)
    for folder in ('build', 'dist', 'ninaivu.egg-info'):
        assert not (tmp_path / folder).exists()

