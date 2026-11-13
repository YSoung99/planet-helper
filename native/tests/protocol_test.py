"""Native helper protocol checks. Never enables an event tap or requests permission."""
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import tempfile
import time

helper = Path(__file__).resolve().parents[1] / 'KeyRepeatHelper'
checks = []

def check(value, name):
    assert value, name
    checks.append(name)

def receive(process):
    ready, _, _ = select.select([process.stdout], [], [], 5)
    assert ready, 'helper status timed out'
    line = process.stdout.readline()
    assert line, f'helper closed unexpectedly: {process.poll()}'
    return json.loads(line)

def send(process, value):
    process.stdin.write(json.dumps(value) + '\n')
    process.stdin.flush()
    expected = value.get('requestId')
    while True:
        status = receive(process)
        if expected is None or status.get('requestId') == expected:
            return status

with tempfile.TemporaryDirectory(prefix='planet-repeat-protocol-') as directory:
    env = dict(os.environ, PLANET_EXP_DATA_DIR=directory)
    process = subprocess.Popen([str(helper)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True, env=env)
    try:
        first = receive(process)
        check(first['type'] == 'status' and first['protocolVersion'] == 1, 'initial status with protocol version')
        check(first['enabled'] is False and first['active'] is False, 'starts disabled without event capture')
        check(first['settings']['attackTriggerKeyCode'] == 0, 'uses existing A trigger setting')
        check(first['postPermissionName'] in ('손쉬운 사용', '기기 제어'), 'OS appropriate permission display name')
        status = send(process, dict(command='configure', enabled=False, firstDelayMs=420,
                                   minIntervalMs=80, maxIntervalMs=150, attackTriggerKeyCode=0, requestId='config'))
        check(status['settings']['firstDelayMs'] == 420 and not status['enabled'], 'timing configure remains disabled')
        invalid = send(process, dict(command='configure', minIntervalMs=300, maxIntervalMs=100, requestId='invalid'))
        check(bool(invalid['error']) and invalid['settings']['minIntervalMs'] == 80, 'invalid interval rejected atomically')
        invalid = send(process, dict(command='configure', firstDelayMs=True, requestId='bool'))
        check(bool(invalid['error']) and invalid['settings']['firstDelayMs'] == 420, 'boolean timing rejected')
        invalid = send(process, dict(command='configure', enabled=1, requestId='enabled-int'))
        check(bool(invalid['error']) and not invalid['enabled'], 'integer enable flag rejected')
        invalid = send(process, dict(command='configure', firstDelayMs=420.5, requestId='float'))
        check(bool(invalid['error']) and invalid['settings']['firstDelayMs'] == 420, 'fractional timing rejected')
        unknown = send(process, dict(command='unknown', requestId='unknown'))
        check(bool(unknown['error']) and not unknown['enabled'], 'unknown command fails without activation')
        process.stdin.write('broken-json\n'); process.stdin.flush()
        check(bool(receive(process)['error']), 'malformed JSON fails without activation')
        duplicate = subprocess.run([str(helper)], input='', capture_output=True, text=True, env=env, timeout=5)
        check(duplicate.returncode == 2 and bool(json.loads(duplicate.stdout)['error']), 'second helper rejected by lock')
        process.stdin.close()
        check(process.wait(timeout=5) == 0, 'stdin EOF exits cleanly')
        follow = subprocess.Popen([str(helper)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True, env=env)
        try:
            check(not receive(follow)['enabled'], 'released lock allows a fresh disabled helper')
            follow.send_signal(signal.SIGTERM)
            check(follow.wait(timeout=5) == 0, 'termination signal exits through cleanup')
        finally:
            if follow.poll() is None: follow.kill(); follow.wait()
    finally:
        if process.poll() is None: process.kill(); process.wait()
    # Keep the input pipe open in this process after the immediate parent exits.
    # EOF cannot explain the helper exit, so this exercises the parent watchdog.
    read_fd, write_fd = os.pipe()
    launch = subprocess.Popen(
        [sys.executable, '-c',
         'import os, subprocess, sys; p = subprocess.Popen([sys.argv[1]], stdin=0); print("CHILD", p.pid, flush=True); os._exit(0)',
         str(helper)], stdin=read_fd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, env=env)
    os.close(read_fd)
    try:
        child_line = launch.stdout.readline()
        check(child_line.startswith('CHILD '), 'watchdog fixture launched with inherited open stdin')
        launch.wait(timeout=5)
        saw_disabled = False
        deadline = time.monotonic() + 5
        exited = False
        while time.monotonic() < deadline:
            ready, _, _ = select.select([launch.stdout], [], [], 0.2)
            if not ready: continue
            line = launch.stdout.readline()
            if not line:
                exited = True
                break
            saw_disabled |= json.loads(line)['enabled'] is False
        check(exited and saw_disabled, 'parent exit stops helper while stdin remains open')
    finally:
        os.close(write_fd)
        if launch.poll() is None: launch.kill(); launch.wait()

for name in checks:
    print('PASS:', name)
print(f'{len(checks)} native protocol checks passed; no event tap or actual input created.')
