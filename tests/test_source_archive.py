import subprocess
from zipfile import ZipFile
from tools.build_source_archive import build_archive


def test_source_archive_uses_only_committed_files(tmp_path):
    def git(*args):subprocess.run(['git',*args],cwd=tmp_path,check=True,capture_output=True)
    git('init')
    (tmp_path/'pyproject.toml').write_text('[project]\nversion = "2.0.0"\n')
    (tmp_path/'source.py').write_text('committed = True\n')
    git('add','pyproject.toml','source.py')
    git('-c','user.name=Ninaivu Test','-c','user.email=test@example.invalid','commit','-m','Synthetic fixture')
    (tmp_path/'source.py').write_text('uncommitted = True\n')
    (tmp_path/'.ai-models').mkdir();(tmp_path/'.ai-models/private.bin').write_bytes(b'not for distribution')
    output=build_archive(tmp_path)
    with ZipFile(output) as archive:
        assert archive.read('Ninaivu-2.0.0/source.py').decode().splitlines()==['committed = True']
        assert not any('.ai-models' in name for name in archive.namelist())
