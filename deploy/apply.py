"""Установка подготовленного релиза на сервере с проверкой systemd и откатом."""
import fcntl
import json
import os
from pathlib import Path
import pwd
import re
import signal
import shutil
import subprocess
import sys
import tempfile
import time

APP_DIR = Path('/opt/bgpwatch')
UNIT = Path('/etc/systemd/system/bgpwatch.service')
BACKUPS = Path('/var/backups/bgpwatch')
SERVICE = 'bgpwatch.service'
HEALTH_TIMEOUT = 180
STABLE_SECONDS = 5
RELEASE_FILES = ('bgpwatch', 'deploy', 'requirements.txt')


def run(*args):
    return subprocess.check_output(args, text=True, stderr=subprocess.STDOUT)


def service_state():
    result = subprocess.run(
        ['systemctl', 'show', SERVICE, '--property=LoadState,ActiveState,SubState,ExecStart,MainPID,NRestarts,InvocationID'],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    state = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
    if result.returncode and state.get('LoadState') != 'not-found':
        raise RuntimeError('Не удалось прочитать состояние systemd')
    return state


def runtime_python(state, app_dir):
    match = re.search(r'\bpath=([^ ;]+)', state.get('ExecStart', ''))
    if state.get('LoadState') != 'not-found':
        if not match:
            raise RuntimeError('Не удалось определить Python из ExecStart')
        python = Path(match.group(1))
        if not (python.parent.parent / 'pyvenv.cfg').is_file():
            raise RuntimeError('ExecStart должен использовать Python из venv')
        return python
    return app_dir / '.venv/bin/python'


def copy_path(source, target):
    if source.is_symlink():
        target.symlink_to(os.readlink(source))
    elif source.is_dir():
        shutil.copytree(source, target, symlinks=True)
    else:
        shutil.copy2(source, target)
    paths = [source]
    if source.is_dir() and not source.is_symlink():
        paths.extend(source.rglob('*'))
    for old in paths:
        new = target if old == source else target / old.relative_to(source)
        info = old.lstat()
        os.chown(new, info.st_uid, info.st_gid, follow_symlinks=False)


def remove_path(path):
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    elif path.exists() or path.is_symlink():
        path.unlink()


def wait_ready(previous_pid):
    deadline = time.monotonic() + HEALTH_TIMEOUT
    stable_since = None
    candidate_pid = None
    while time.monotonic() < deadline:
        state = service_state()
        pid = state.get('MainPID', '0')
        if state.get('ActiveState') == 'failed' or int(state.get('NRestarts', '0')):
            raise RuntimeError('Сервис упал или автоматически перезапустился')
        if pid != '0' and pid != previous_pid:
            candidate_pid = candidate_pid or pid
            if pid != candidate_pid:
                raise RuntimeError('Процесс сменился во время проверки')
        invocation = state.get('InvocationID')
        messages = []
        if invocation and candidate_pid:
            entries = run('journalctl', '_SYSTEMD_INVOCATION_ID=' + invocation, '-o', 'json', '--no-pager')
            messages = [entry.get('MESSAGE', '') for line in entries.splitlines()
                        if line and (entry := json.loads(line)).get('_PID') == candidate_pid]
        if any('ошибка polling' in msg or 'ошибка цикла мониторинга' in msg or 'Traceback' in msg for msg in messages):
            raise RuntimeError('После запуска приложение сообщило об ошибке')
        startup = any('запущен как @' in msg and not msg.endswith('@None') for msg in messages)
        cycle = any('цикл мониторинга:' in msg for msg in messages)
        if state.get('ActiveState') == 'active' and state.get('SubState') == 'running' and startup and cycle:
            stable_since = stable_since or time.monotonic()
            if time.monotonic() - stable_since >= STABLE_SECONDS:
                return pid
        else:
            stable_since = None
        time.sleep(1)
    raise RuntimeError('Запуск Telegram и первый цикл мониторинга не подтверждены за 180 секунд')


def deploy(stage, app_dir=APP_DIR, unit=UNIT, backups=BACKUPS):
    state = service_state()
    was_active = state.get('ActiveState') == 'active'
    if state.get('ActiveState') not in ('active', 'inactive', 'failed'):
        raise RuntimeError('Сервис уже запускается или останавливается')
    python = runtime_python(state, app_dir)
    venv = python.parent.parent
    if venv.is_symlink():
        raise RuntimeError('Симлинк venv требует отдельного способа отката')
    for name in RELEASE_FILES:
        if not (stage / name).exists():
            raise RuntimeError('Подготовленный релиз неполон: ' + name)
    backups.mkdir(parents=True, exist_ok=True)
    backup = Path(tempfile.mkdtemp(prefix=time.strftime('%Y%m%d-%H%M%S-'), dir=backups))
    os.chmod(backup, 0o700)
    targets = {name: app_dir / name for name in RELEASE_FILES}
    targets.update(venv=venv, unit=unit)
    for name, target in targets.items():
        if target.exists():
            copy_path(target, backup / name)
    print('Резервная копия: ' + str(backup), flush=True)
    mutation_started = False
    try:
        if was_active:
            run('systemctl', 'stop', SERVICE)
        mutation_started = True
        app_dir.mkdir(parents=True, exist_ok=True)
        try:
            user = pwd.getpwnam('bgpwatch')
        except KeyError:
            run('useradd', '--system', '--home', str(app_dir), '--shell', '/usr/sbin/nologin', 'bgpwatch')
            user = pwd.getpwnam('bgpwatch')
        os.chown(app_dir, user.pw_uid, user.pw_gid)
        if not python.exists():
            run(sys.executable, '-m', 'venv', str(venv))
            run('chown', '-R', f'{user.pw_uid}:{user.pw_gid}', str(venv))
        run(str(python), '-m', 'pip', 'install', '--disable-pip-version-check', '-r', str(stage / 'requirements.txt'))
        run(str(python), '-m', 'pip', 'check')
        for name in RELEASE_FILES:
            remove_path(app_dir / name)
            copy_path(stage / name, app_dir / name)
            run('chown', '-R', f'{user.pw_uid}:{user.pw_gid}', str(app_dir / name))
        unit.parent.mkdir(parents=True, exist_ok=True)
        remove_path(unit)
        unit.write_text((stage / 'deploy/bgpwatch.service').read_text().replace(
            'ExecStart=/opt/bgpwatch/.venv/bin/python -m bgpwatch',
            f'ExecStart={python} -m bgpwatch',
        ))
        os.chmod(unit, 0o644)
        run('systemctl', 'daemon-reload')
        if was_active:
            run('systemctl', 'start', SERVICE)
            pid = wait_ready(state.get('MainPID', '0'))
            print('Готово: Telegram и мониторинг работают, PID ' + pid, flush=True)
        else:
            print('Релиз установлен. После настройки .env: systemctl enable --now bgpwatch', flush=True)
    except BaseException:
        if mutation_started:
            if service_state().get('LoadState') != 'not-found':
                try:
                    run('systemctl', 'stop', SERVICE)
                except subprocess.CalledProcessError:
                    if service_state().get('MainPID', '0') != '0':
                        raise
            for name, target in targets.items():
                remove_path(target)
                if (backup / name).exists():
                    copy_path(backup / name, target)
            run('systemctl', 'daemon-reload')
            if not was_active:
                print('Прежние файлы восстановлены; сервис оставлен остановленным', flush=True)
        if was_active:
            current = service_state()
            if current.get('ActiveState') == 'active' and current.get('MainPID') == state.get('MainPID'):
                print('Прежний сервис продолжает работать', flush=True)
            else:
                run('systemctl', 'start', SERVICE)
                pid = wait_ready(state.get('MainPID', '0'))
                print('Откат проверен, PID ' + pid, flush=True)
        raise


if __name__ == '__main__':
    def interrupted(signum, frame):
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGHUP, interrupted)
    signal.signal(signal.SIGTERM, interrupted)
    try:
        lock_dir = Path('/run/bgpwatch-deploy')
        lock_dir.mkdir(mode=0o700, exist_ok=True)
        with (lock_dir / 'lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            deploy(Path(sys.argv[1]))
    except Exception as error:
        print('Деплой не выполнен: ' + str(error), file=sys.stderr)
        sys.exit(1)
