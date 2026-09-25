from pathlib import Path
import subprocess
import shutil
import sys
ROOT=Path(__file__).resolve().parents[1]

def test_initializer_preserves_existing_profile(tmp_path):
    shutil.copytree(ROOT/'examples',tmp_path/'examples')
    (tmp_path/'scripts').mkdir()
    shutil.copy(ROOT/'scripts/init_workspace.py',tmp_path/'scripts/init_workspace.py')
    (tmp_path/'profile').mkdir()
    own=tmp_path/'profile/notes.md';own.write_text('user-owned')
    subprocess.run([sys.executable,str(tmp_path/'scripts/init_workspace.py')],check=True)
    assert own.read_text()=='user-owned'
    assert len(list((tmp_path/'profile').iterdir()))>1

def test_agent_runbook_and_privacy_defaults_exist():
    assert 'SKILL.md' in (ROOT/'AGENTS.md').read_text()
    assert '## Session start' in (ROOT/'SKILL.md').read_text()
    ignores=(ROOT/'.gitignore').read_text().splitlines()
    assert {'/profile/','/data/','/build/','.env'} <= set(ignores)
