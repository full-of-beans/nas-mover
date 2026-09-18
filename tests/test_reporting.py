import json
from pathlib import Path

import pytest

from nas_mover.models import Branch, PlannedMove
from nas_mover.reporting import ExecutionReport, render
from nas_mover.transfer import MoveCancelled, execute_move
from nas_mover.executor import execute_moves
from nas_mover.cli import run


def moves():
    a, b = [Branch(Path('/' + n), i, False, 100, 10, 90) for i, n in enumerate(('a', 'b'))]
    c, d = [Branch(Path('/' + n), i + 2, True, 100, 100, 0) for i, n in enumerate(('c', 'd'))]
    return [PlannedMove(a, d, Path('one'), 90, 0, 0, 'test'),
            PlannedMove(b, c, Path('two'), 10, 0, 0, 'test'),
            PlannedMove(a, c, Path('three'), 0, 0, 0, 'test')]


def test_contract_routes_progress_and_throttling(monkeypatch):
    events = []
    report = ExecutionReport(live=True, emit=events.append)
    plan = moves()
    report.plan(plan)
    assert report.snapshot()['planned'] == {'files': 3, 'bytes': 100, 'routes': [
        {'source': '/a', 'destination': '/c', 'files': 1, 'bytes': 0},
        {'source': '/a', 'destination': '/d', 'files': 1, 'bytes': 90},
        {'source': '/b', 'destination': '/c', 'files': 1, 'bytes': 10}]}
    def transfer(move, **kwargs):
        snap = report.snapshot()
        assert snap['dispositions']['active']['files'] == 1
        return move.size + 2
    wrapped = report.wrap(transfer)
    wrapped(plan[0])
    snap = report.snapshot()
    assert snap['completed']['bytes'] == 92
    assert snap['progress_percent'] == snap['byte_progress_percent'] == 90
    assert snap['file_progress_percent'] == 100 / 3
    wrapped(plan[1])
    wrapped(plan[2])
    result = report.finish('completed')
    assert result['dispositions']['completed']['bytes'] == 100
    assert result['completed']['bytes'] == 106
    assert result['progress_percent'] == 100
    assert result['eta_seconds'] is None
    assert [e['event'] for e in events] == ['planned', 'progress', 'result']
    assert json.loads(json.dumps(result)) == result
    assert 'TOTAL: planned 3 files / 100 bytes; completed 3 files / 106 bytes' in render(result)


@pytest.mark.parametrize('exc,status', [(RuntimeError('broken'), 'failed'), (MoveCancelled('stop'), 'cancelled')])
def test_partial_terminal(exc, status):
    report = ExecutionReport(live=True)
    plan = moves()
    report.plan(plan)
    report.wrap(lambda move, **kw: None)(plan[0])
    with pytest.raises(type(exc)):
        report.wrap(lambda move, **kw: (_ for _ in ()).throw(exc))(plan[1])
    result = report.finish(status, str(exc))
    assert result['completed']['files'] == 1
    assert result['dispositions']['not_started']['files'] == 1
    assert result['dispositions']['interrupted' if status == 'cancelled' else 'failed']['files'] == 1
    assert str(exc) in render(result)


def test_empty_zero_bytes_unknown_plan():
    report = ExecutionReport(live=False)
    assert report.snapshot()['planned'] is None
    assert report.snapshot()['progress_percent'] is None
    assert render(report.finish('failed', 'x')).endswith('ERROR: x')
    report.plan([])
    assert report.snapshot()['progress_percent'] == 100
    plan = [moves()[2]]
    report.plan(plan)
    assert report.snapshot()['progress_percent'] == 0
    report.wrap(lambda move: None)(plan[0])
    assert report.snapshot()['progress_percent'] == 100


def setup_cli(tmp_path, monkeypatch, plan):
    from nas_mover.discovery import Pool
    branches = [Branch(tmp_path / n, i, i == 2, 100, 100, 0) for i, n in enumerate(('a', 'b', 'c'))]
    monkeypatch.setattr('nas_mover.cli.discover_runtime_pool', lambda mount: Pool(mount, [], {}, 0))
    monkeypatch.setattr('nas_mover.cli.discover_branches', lambda pool: branches)
    monkeypatch.setattr('nas_mover.cli.plan_moves', lambda *a, **k: plan)
    return ['--lock', str(tmp_path / 'lock')]


@pytest.mark.parametrize('option', ['--json', '--json-events'])
@pytest.mark.parametrize('live', [False, True])
def test_cli_machine_success(tmp_path, monkeypatch, capsys, option, live):
    plan = moves()
    args = setup_cli(tmp_path, monkeypatch, plan)
    monkeypatch.setattr('nas_mover.cli.execute_move', lambda move, **kw: move.size)
    assert run(args + [option] + (['--live'] if live else [])) == 0
    output = capsys.readouterr()
    assert not output.err
    objects = [json.loads(line) for line in output.out.splitlines()]
    result = objects[-1] if option == '--json' else objects[-1]['result']
    assert result['schema_version'] == 1
    assert result['status'] == ('completed' if live else 'planned')
    assert result['completed']['files'] == (3 if live else 0)
    if live:
        assert result['planned'] == result['completed']
    if option == '--json-events':
        assert objects[0]['event'] == 'planned'
        assert objects[-1]['event'] == 'result'


@pytest.mark.parametrize('option', ['--json', '--json-events', 'human'])
@pytest.mark.parametrize('cancel', [True, False])
def test_cli_execution_error(tmp_path, monkeypatch, capsys, option, cancel):
    args = setup_cli(tmp_path, monkeypatch, [moves()[0]])
    exc = MoveCancelled('stop') if cancel else RuntimeError('broken')
    monkeypatch.setattr('nas_mover.cli.execute_move', lambda *a, **kw: (_ for _ in ()).throw(exc))
    if option == 'human':
        with pytest.raises(type(exc)):
            run(args + ['--live'])
        assert ('CANCELLED' if cancel else 'FAILED') in capsys.readouterr().out
    else:
        assert run(args + ['--live', option]) == (130 if cancel else 1)
        output = capsys.readouterr()
        obj = json.loads(output.out.splitlines()[-1])
        result = obj if option == '--json' else obj['result']
        assert result['status'] == ('cancelled' if cancel else 'failed')
        assert result['completed']['files'] == 0
        assert str(exc) in output.err


def test_cli_error_before_plan(capsys):
    assert run(['--json', '--scope', '../bad']) == 1
    result = json.loads(capsys.readouterr().out)
    assert result['planned'] is None
    assert result['status'] == 'failed'


def test_actual_transfer_boundary_and_changed_plan_size(tmp_path):
    source = Branch(tmp_path / 'a', 0, False, 100, 10, 90)
    dest = Branch(tmp_path / 'b', 1, False, 100, 10, 90)
    source.path.mkdir()
    (source.path / 'file').write_bytes(b'actual')
    move = PlannedMove(source, dest, Path('file'), 1, 0, 0, 'test')
    report = ExecutionReport(live=True)
    report.plan([move])
    execute_moves([move], move_executor=report.wrap(execute_move))
    result = report.finish('completed')
    assert result['planned']['bytes'] == 1
    assert result['completed']['bytes'] == 6
    assert not move.source_path.exists()
    assert move.destination_path.read_bytes() == b'actual'


def test_parallel_failure_cannot_be_masked_by_peer_cancellation(tmp_path):
    import threading
    plan = moves()[:2]
    barrier = threading.Barrier(2)
    def transfer(move, **kwargs):
        barrier.wait(timeout=2)
        if move is plan[0]:
            raise MoveCancelled('peer stopped')
        raise RuntimeError('actual failure')
    report = ExecutionReport(live=True)
    report.plan(plan)
    with pytest.raises(RuntimeError, match='actual failure'):
        execute_moves(plan, move_executor=report.wrap(transfer))
    result = report.finish('failed')
    assert result['dispositions']['failed']['files'] == 1
    assert result['dispositions']['interrupted']['files'] == 1
