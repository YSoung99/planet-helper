"""Generate an adapter from production code with all actual event posting replaced."""
from pathlib import Path
base = Path(__file__).resolve().parents[1]
engine = (base / 'RepeatEngine.swift').read_text()
assert engine.count('CGEventSource(stateID: .privateState)') == 1
assert 'options: .defaultTap' in engine
assert 'return suppress ? nil : Unmanaged.passUnretained(event)' in engine
engine = engine.replace('Permissions(listen: CGPreflightListenEventAccess(), post: CGPreflightPostEventAccess())',
                        'Permissions(listen: testListen, post: testPost)')
engine = engine.replace('NSWorkspace.shared.frontmostApplication?.bundleIdentifier == targetBundleID', 'testTarget')
assert engine.count('if resume { start() }') == 1
engine = engine.replace('if resume { start() }', 'if resume { testResumeAfterSettings() }')
assert engine.count('down.post(tap: .cghidEventTap)') == 1
assert engine.count('up.post(tap: .cghidEventTap)') == 1
engine = engine.replace('down.post(tap: .cghidEventTap)', 'recordEvent(down)')
engine = engine.replace('up.post(tap: .cghidEventTap)', 'recordEvent(up)')
assert '.post(tap:' not in engine
(base / 'tests/adapter').mkdir(exist_ok=True)
(base / 'tests/adapter/main.swift').write_text(
    engine + '\nfunc legacyRepeaterIsRunning() -> Bool { testLegacyRunning }\n' +
    (base / 'tests/adapter-footer.swift').read_text())
print('Production engine adapter generated: actual event posting, permission and focus reads are stubbed.')
