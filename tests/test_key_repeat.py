import json
import os
import tempfile
import time
import unittest
from pathlib import Path

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtCore import QProcess
from PySide6.QtWidgets import QApplication
from key_repeat import DEFAULT_SETTINGS, KeyRepeatBridge, validated_settings


class RepeatSettingsTests(unittest.TestCase):
    def test_settings_never_restore_arming_and_reject_unsafe_intervals(self):
        selected = validated_settings(dict(DEFAULT_SETTINGS, enabled=True))
        self.assertNotIn('enabled', selected)
        for change in ({'firstDelayMs': 0}, {'minIntervalMs': 49}, {'maxIntervalMs': 1001},
                       {'minIntervalMs': 121}, {'firstDelayMs': True}, {'attackTriggerKeyCode': 7}):
            with self.assertRaises(ValueError):
                validated_settings(dict(DEFAULT_SETTINGS, **change))


class RepeatBridgeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.path = Path(self.folder.name)
        self.log = self.path/'commands.jsonl'
        self.helper = self.path/'helper'
        self.helper.write_text('''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
state = dict(type='status',enabled=False,active=False,accessibility=True,inputMonitoring=True,
             legacyRunning=False,status='off',error='',settings=dict(firstDelayMs=230,minIntervalMs=85,
             maxIntervalMs=120,attackTriggerKeyCode=0))
log=Path(__file__).with_name('commands.jsonl')
Path(__file__).with_name('environment.txt').write_text(os.environ.get('PLANET_EXP_DATA_DIR',''))
def emit(): print(json.dumps(state),flush=True)
emit()
for line in sys.stdin:
    message=json.loads(line)
    with log.open('a') as stream: stream.write(json.dumps(message)+'\\n')
    if message['command']=='shutdown': break
    if message['command']=='configure':
        state['enabled']=message['enabled']
        state['settings']={key:message[key] for key in state['settings']}
    if message['command']=='malformed':
        print('{broken',flush=True)
    elif message['command']=='missing-field':
        print(json.dumps(dict(type='status',enabled=True)),flush=True)
    else: emit()
''')
        self.helper.chmod(0o755)
        self.bridge = None

    def tearDown(self):
        if self.bridge:
            self.bridge.shutdown()
        self.folder.cleanup()

    def wait_for(self, condition, seconds=4):
        deadline = time.monotonic()+seconds
        while not condition() and time.monotonic() < deadline:
            self.application.processEvents()
            time.sleep(.01)
        self.assertTrue(condition())

    def commands(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def create_bridge(self):
        self.bridge = KeyRepeatBridge(self.path, self.helper)
        self.bridge.start()
        self.wait_for(lambda: self.bridge.state.get('status') == 'off')
        return self.bridge

    def test_start_with_saved_enabled_true_stays_off_and_keeps_settings(self):
        (self.path/'key-repeat.json').write_text(json.dumps(dict(DEFAULT_SETTINGS, firstDelayMs=300,enabled=True)))
        bridge=self.create_bridge()
        self.wait_for(lambda: bridge.settings['firstDelayMs']==300 and self.log.exists())
        self.assertFalse(bridge.state['enabled'])
        self.assertTrue(all(not row.get('enabled',False) for row in self.commands()))
        self.assertFalse(any(row['command']=='request_permissions' for row in self.commands()))
        self.assertEqual((self.path/'environment.txt').read_text(),str(self.path.resolve()))
        bridge.shutdown()
        self.assertEqual(bridge.process.state(), QProcess.ProcessState.NotRunning)
        self.assertEqual(self.commands()[-1]['command'], 'shutdown')

    def test_invalid_packet_stops_helper_instead_of_only_changing_display(self):
        bridge=self.create_bridge()
        bridge.send('malformed')
        self.wait_for(lambda: bridge.closing)
        self.assertEqual(bridge.process.state(), QProcess.ProcessState.NotRunning)
        self.assertFalse(bridge.state['enabled'])
        self.assertIn('종료',bridge.state['error'])
        self.assertEqual(self.commands()[-1]['command'], 'shutdown')

    def test_missing_helper_leaves_repeat_disabled_without_permission_request(self):
        bridge=KeyRepeatBridge(self.path,self.path/'missing')
        self.bridge=bridge
        bridge.start()
        self.assertFalse(bridge.state['enabled'])
        self.assertTrue(bridge.state['error'])
        self.assertEqual(bridge.process.state(),QProcess.ProcessState.NotRunning)

    def test_status_with_missing_boolean_fields_stops_helper(self):
        bridge=self.create_bridge()
        bridge.send('missing-field')
        self.wait_for(lambda: bridge.closing)
        self.assertEqual(bridge.process.state(),QProcess.ProcessState.NotRunning)
        self.assertFalse(bridge.state['enabled'])
        self.assertTrue(bridge.state['error'])

    def test_configuration_persists_only_settings_and_stops_repeat(self):
        bridge=self.create_bridge()
        bridge.configure(dict(DEFAULT_SETTINGS, minIntervalMs=100,maxIntervalMs=150))
        self.wait_for(lambda: bridge.settings['minIntervalMs']==100 and self.log.exists())
        saved=json.loads((self.path/'key-repeat.json').read_text())
        self.assertNotIn('enabled',saved)
        self.assertEqual(saved['maxIntervalMs'],150)
        self.wait_for(lambda: any(row['command']=='configure' for row in self.commands()))
        self.assertFalse(next(row for row in self.commands() if row['command']=='configure')['enabled'])

    def test_unexpected_exit_disables_and_does_not_relaunch(self):
        bridge=self.create_bridge()
        bridge.process.kill()
        self.wait_for(lambda: bridge.process.state()==QProcess.ProcessState.NotRunning)
        self.application.processEvents()
        self.assertFalse(bridge.state['enabled'])
        self.assertTrue(bridge.state['error'])
        self.assertFalse(bridge.poll_timer.isActive())


if __name__=='__main__': unittest.main()
