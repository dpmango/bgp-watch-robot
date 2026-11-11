import json
import os
import pwd
import subprocess

import pytest

from deploy import apply


@pytest.fixture
def deployment(tmp_path, monkeypatch):
    app = tmp_path / 'app'
    stage = tmp_path / 'stage'
    unit = tmp_path / 'systemd/bgpwatch.service'
    unit.parent.mkdir()
    unit.write_text('previous unit')
    for root, version in [(app, 'old'), (stage, 'new')]:
        (root / 'bgpwatch').mkdir(parents=True)
        (root / 'bgpwatch/__init__.py').write_text(version)
        (root / 'deploy').mkdir()
        (root / 'deploy/bgpwatch.service').write_text('ExecStart=/opt/bgpwatch/.venv/bin/python -m bgpwatch\n')
        (root / 'requirements.txt').write_text(version)
    venv = app / '.venv-security'
    (venv / 'bin').mkdir(parents=True)
    (venv / 'bin/python').touch()
    (venv / 'pyvenv.cfg').touch()
    (venv / 'package').write_text('old')
    (app / '.env').write_text('secret')
    (app / 'bgpwatch.db').write_bytes(b'database')
    (app / '.venv').mkdir()
    (app / '.venv/package').write_text('unused')
    state = {'LoadState': 'loaded', 'ActiveState': 'active', 'MainPID': '7',
             'ExecStart': '{ path=' + str(venv / 'bin/python') + ' ; argv[]=python -m bgpwatch ; }'}
    monkeypatch.setattr(apply, 'service_state', lambda: state.copy())
    monkeypatch.setattr(apply.pwd, 'getpwnam', lambda name: pwd.getpwuid(os.getuid()))
    calls = []
    failure = {'phase': None}

    def run(*args):
        calls.append(args)
        if args[:2] == ('systemctl', 'stop'):
            state.update(ActiveState='inactive', MainPID='0')
        elif args[:2] == ('systemctl', 'start'):
            state.update(ActiveState='active', MainPID='8')
        if args[1:4] == ('-m', 'pip', 'install'):
            (venv / 'package').write_text('new')
            (venv / 'added-package').touch()
            if failure['phase'] == 'install':
                raise subprocess.CalledProcessError(1, args)
        return ''

    def ready(previous_pid):
        assert previous_pid == '7'
        if failure['phase'] == 'startup' and (app / 'bgpwatch/__init__.py').read_text() == 'new':
            raise RuntimeError('startup failed')
        return '8'

    monkeypatch.setattr(apply, 'run', run)
    monkeypatch.setattr(apply, 'wait_ready', ready)
    return app, stage, unit, venv, state, calls, failure


def test_deploy_updates_effective_venv_and_preserves_data(deployment, tmp_path):
    app, stage, unit, venv, state, calls, failure = deployment
    apply.deploy(stage, app, unit, tmp_path / 'backups')
    assert any(args[:4] == (str(venv / 'bin/python'), '-m', 'pip', 'install') for args in calls)
    assert str(venv / 'bin/python') in unit.read_text()
    assert (app / 'bgpwatch/__init__.py').read_text() == 'new'
    assert (venv / 'package').read_text() == 'new'
    assert (app / '.venv/package').read_text() == 'unused'
    assert (app / '.env').read_text() == 'secret'
    assert (app / 'bgpwatch.db').read_bytes() == b'database'


@pytest.mark.parametrize('phase', ['install', 'startup'])
def test_deploy_failure_restores_code_packages_unit_and_restarts(deployment, tmp_path, phase):
    app, stage, unit, venv, state, calls, failure = deployment
    failure['phase'] = phase
    with pytest.raises((RuntimeError, subprocess.CalledProcessError)):
        apply.deploy(stage, app, unit, tmp_path / 'backups')
    assert (app / 'bgpwatch/__init__.py').read_text() == 'old'
    assert (app / 'requirements.txt').read_text() == 'old'
    assert (venv / 'package').read_text() == 'old'
    assert not (venv / 'added-package').exists()
    assert unit.read_text() == 'previous unit'
    assert calls[-1] == ('systemctl', 'start', apply.SERVICE)
    assert (app / '.env').read_text() == 'secret'
    assert (app / 'bgpwatch.db').read_bytes() == b'database'


def test_stop_error_after_stopping_recovers_old_service(deployment, tmp_path, monkeypatch):
    app, stage, unit, venv, state, calls, failure = deployment
    real_run = apply.run

    def stop_error(*args):
        if args[:2] == ('systemctl', 'stop'):
            state.update(ActiveState='inactive', MainPID='0')
            raise subprocess.CalledProcessError(1, args)
        return real_run(*args)

    monkeypatch.setattr(apply, 'run', stop_error)
    with pytest.raises(subprocess.CalledProcessError):
        apply.deploy(stage, app, unit, tmp_path / 'backups')
    assert calls == [('systemctl', 'start', apply.SERVICE)]
    assert (app / 'bgpwatch/__init__.py').read_text() == 'old'
    assert (venv / 'package').read_text() == 'old'


def test_rollback_preserves_owners_groups_and_private_modes(deployment, tmp_path):
    app, stage, unit, venv, state, calls, failure = deployment
    groups = [gid for gid in os.getgroups() if gid != os.getgid()]
    if not groups:
        pytest.skip('нужна дополнительная группа для проверки восстановления gid')
    paths = [app / 'bgpwatch', app / 'bgpwatch/__init__.py', venv / 'pyvenv.cfg']
    for path in paths:
        os.chown(path, os.getuid(), groups[0])
        path.chmod(0o700 if path.is_dir() else 0o600)
    before = [(p.stat().st_uid, p.stat().st_gid, p.stat().st_mode) for p in paths]
    failure['phase'] = 'startup'
    with pytest.raises(RuntimeError):
        apply.deploy(stage, app, unit, tmp_path / 'backups')
    assert [(p.stat().st_uid, p.stat().st_gid, p.stat().st_mode) for p in paths] == before


def test_deploy_keeps_inactive_service_stopped(deployment, tmp_path):
    app, stage, unit, venv, state, calls, failure = deployment
    state['ActiveState'] = 'inactive'
    apply.deploy(stage, app, unit, tmp_path / 'backups')
    assert not any(args[:2] in [('systemctl', 'start'), ('systemctl', 'stop')] for args in calls)


@pytest.mark.parametrize('fail', [False, True])
def test_first_install_private_umask_and_failed_pip(tmp_path, monkeypatch, fail):
    app = tmp_path / 'app'
    stage = tmp_path / 'stage'
    (stage / 'bgpwatch').mkdir(parents=True)
    (stage / 'deploy').mkdir()
    (stage / 'deploy/bgpwatch.service').write_text('ExecStart=/opt/bgpwatch/.venv/bin/python -m bgpwatch\n')
    (stage / 'requirements.txt').write_text('new')
    monkeypatch.setattr(apply, 'service_state', lambda: {'LoadState': 'not-found', 'ActiveState': 'inactive'})
    monkeypatch.setattr(apply.pwd, 'getpwnam', lambda name: pwd.getpwuid(os.getuid()))

    calls = []

    def run(*args):
        calls.append(args)
        if args[1:3] == ('-m', 'venv'):
            (app / '.venv/bin').mkdir(parents=True)
            (app / '.venv/bin/python').touch()
        elif args[1:4] == ('-m', 'pip', 'install') and fail:
            raise subprocess.CalledProcessError(1, args)
        return ''

    monkeypatch.setattr(apply, 'run', run)
    unit = tmp_path / 'systemd/bgpwatch.service'
    old_umask = os.umask(0o077)
    try:
        if fail:
            with pytest.raises(subprocess.CalledProcessError):
                apply.deploy(stage, app, unit, tmp_path / 'backups')
        else:
            apply.deploy(stage, app, unit, tmp_path / 'backups')
    finally:
        os.umask(old_umask)
    assert not any(args[:2] in [('systemctl', 'stop'), ('systemctl', 'start')] for args in calls)
    assert ('chown', '-R', f'{os.getuid()}:{os.getgid()}', str(app / '.venv')) in calls
    assert (app / '.venv').exists() is not fail
    assert unit.exists() is not fail


def test_wait_ready_ignores_old_process_and_waits_for_full_cycle(monkeypatch):
    clock = {'now': 0}
    monkeypatch.setattr(apply.time, 'monotonic', lambda: clock['now'])
    monkeypatch.setattr(apply.time, 'sleep', lambda seconds: clock.update(now=clock['now'] + seconds))
    monkeypatch.setattr(apply, 'STABLE_SECONDS', 2)
    state = {'ActiveState': 'active', 'SubState': 'running', 'MainPID': '20',
             'NRestarts': '0', 'InvocationID': 'new-invocation'}
    monkeypatch.setattr(apply, 'service_state', lambda: state)

    def journal(*args):
        assert args[1] == '_SYSTEMD_INVOCATION_ID=new-invocation'
        pid = '7' if clock['now'] < 3 else '20'
        return '\n'.join(json.dumps({'_PID': pid, 'MESSAGE': msg}) for msg in [
            'INFO bgpwatch запущен как @BGPWatchRobot', 'INFO bgpwatch цикл мониторинга: 0 событий',
        ])

    monkeypatch.setattr(apply, 'run', journal)
    assert apply.wait_ready('7') == '20'
    assert clock['now'] == 5


@pytest.mark.parametrize('messages,restarts', [
    (['INFO bgpwatch запущен как @None', 'INFO bgpwatch цикл мониторинга: 0 событий'], '0'),
    (['ERROR bgpwatch ошибка polling'], '0'),
    ([], '1'),
])
def test_wait_ready_rejects_invalid_startup_errors_and_restarts(monkeypatch, messages, restarts):
    clock = {'now': 0}
    monkeypatch.setattr(apply.time, 'monotonic', lambda: clock['now'])
    monkeypatch.setattr(apply.time, 'sleep', lambda seconds: clock.update(now=clock['now'] + seconds))
    monkeypatch.setattr(apply, 'HEALTH_TIMEOUT', 3)
    monkeypatch.setattr(apply, 'service_state', lambda: {
        'ActiveState': 'active', 'SubState': 'running', 'MainPID': '20',
        'NRestarts': restarts, 'InvocationID': 'new-invocation',
    })
    monkeypatch.setattr(apply, 'run', lambda *args: '\n'.join(
        json.dumps({'_PID': '20', 'MESSAGE': msg}) for msg in messages
    ))
    with pytest.raises(RuntimeError):
        apply.wait_ready('7')
