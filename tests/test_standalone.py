from studyplan.config import parent_root
from studyplan.remnote import fetch_catalog
import pytest

def test_no_implicit_parent_workspace(tmp_path,monkeypatch):
    monkeypatch.delenv('JOBRADAR_ROOT',raising=False)
    root=tmp_path/'planner'
    assert parent_root(root)==root/'integrations/job-radar'

def test_explicit_scanner_path(tmp_path,monkeypatch):
    scanner=tmp_path/'scanner'
    monkeypatch.setenv('JOBRADAR_ROOT',str(scanner))
    assert parent_root(tmp_path/'planner')==scanner

def test_remnote_has_no_embedded_account_identifiers():
    with pytest.raises(ValueError,match='No collections'):
        fetch_catalog('http://127.0.0.1:1','test')
