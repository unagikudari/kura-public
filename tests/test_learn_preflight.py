import sys
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from tagger import learn_preflight as probe


def fixtures(monkeypatch, stage=('training',7840), read_only=True):
    importer=Mock()
    monkeypatch.setattr(probe.importlib,'import_module',importer)
    monkeypatch.setattr(probe.importlib.metadata,'version',lambda n:'test')
    reader=Mock(return_value=[('on' if read_only else 'off',)])
    current=Mock(return_value=stage)
    monkeypatch.setitem(sys.modules,'tagger.vnext_runner',SimpleNamespace(kura_read_primary=reader,current_stage=current))
    return importer,reader,current


def test_preflight_imports_all_declared_modules_then_reads_exact_stage(monkeypatch):
    importer,reader,current=fixtures(monkeypatch)
    result=probe.probe('monochrome',7840)
    assert result['ok'] and result['transaction_read_only']
    assert {c.args[0] for c in importer.call_args_list}==set(probe.DEPENDENCIES.values())
    reader.assert_called_once_with('SHOW transaction_read_only')
    current.assert_called_once_with('monochrome')


def test_missing_dependency_stops_before_db(monkeypatch):
    importer,reader,current=fixtures(monkeypatch)
    importer.side_effect=ModuleNotFoundError('psycopg2')
    with pytest.raises(ModuleNotFoundError):probe.probe('monochrome',7840)
    reader.assert_not_called();current.assert_not_called()


@pytest.mark.parametrize('stage', [('approved',7840),('training',7839),(None,0)])
def test_stale_event_stops(monkeypatch,stage):
    fixtures(monkeypatch,stage)
    with pytest.raises(RuntimeError,match='current training'):probe.probe('monochrome',7840)


def test_read_write_connection_is_rejected(monkeypatch):
    _,_,current=fixtures(monkeypatch,read_only=False)
    with pytest.raises(RuntimeError,match='read-only'):probe.probe('monochrome',7840)
    current.assert_not_called()


def test_cli_does_not_leak_driver_exception_credentials(monkeypatch,capsys):
    monkeypatch.setattr(sys,'argv',['preflight','--tag','monochrome','--event','7840'])
    def fail(*a):raise RuntimeError('postgresql://SECRET')
    monkeypatch.setattr(probe,'probe',fail)
    assert probe.main()==1
    assert 'SECRET' not in capsys.readouterr().out


def test_preflight_covers_declared_requirement_names():
    from pathlib import Path
    from packaging.requirements import Requirement
    from packaging.utils import canonicalize_name
    names=set()
    for line in (Path(__file__).resolve().parents[1]/'tagger/requirements.txt').read_text().splitlines():
        line=line.split('#',1)[0].strip()
        if line:names.add(canonicalize_name(Requirement(line).name))
    assert names=={canonicalize_name(n) for n in probe.DEPENDENCIES if n!='torch'}
