# Native repeat service

`KeyRepeatHelper` provides the key engine inside the unified application's package. It has no window, menu, or independent launch flow. Build it with `./build.sh`; the enclosing app build calls that script automatically.

The state machine and event-tap implementation derive from `keyrepeat-trigger-fix/Source` of the existing 2.3.0 application. The initial attack key is A (0), matching the user's saved rollback setting. Z (6) remains an explicit optional setting. Enter (36) and keypad Enter (76) stay independent. The legacy `local.codex.mapleworldkeyrepeat` preference suite is read at startup and never written by this service. The parent application owns persistence of unified settings.

## Protocol

Use one JSON object per stdin line, with `command` equal to `status`, `configure`, `request_permissions`, or `shutdown`. Configure accepts optional `enabled`, `firstDelayMs`, `minIntervalMs`, `maxIntervalMs`, and `attackTriggerKeyCode`. Initial delay permits 100–2000ms, repeat intervals permit 50–1000ms with minimum no greater than maximum, and the trigger must be 0 or 6. Timing fields are integers and `enabled` is a JSON boolean. Optional string or numeric `requestId` is echoed.

Stdout emits newline JSON status snapshots: `type`, `protocolVersion`, `enabled`, `active`, `accessibility`, `inputMonitoring`, `postPermissionName`, `legacyRunning`, `status`, `error`, and `settings`. `accessibility` reports the posting permission, displayed as device control on macOS 27 and later. Status changes use a bounded, coalescing writer to keep a stalled reader outside the event-tap callback. A configure operation emits its final applied state. Invalid control requests stop an enabled engine.

The engine starts disabled. Permission prompts occur only for `request_permissions`. It captures keys only with explicit enablement and both permissions. Physical key release, focus changes, modifiers, permission revocation, session changes, event-tap failures, shutdown, input EOF, termination signals, and parent exit stop or interrupt output as appropriate. Returning focus does not resume an old hold. Launch of the legacy repeat app blocks enablement or stops the active engine. A process lock prevents multiple unified helpers.

The lock resides in `~/Library/Application Support/Maple Planet Helper/repeat-helper.lock`; `PLANET_EXP_DATA_DIR` overrides the directory for isolated diagnostics. Lock-file existence alone is harmless; the advisory lock belongs to the running process.

## Verification

- `tests/state-tests.swift`: 27 state and settings checks.
- `tests/prepare_test_engine.py` generates `tests/adapter/main.swift` directly from production engine code. All event posting, permission queries, and foreground queries are replaced with test functions; no event tap is created. `tests/adapter-footer.swift` supplies 87 behavioral checks, including timing, balanced releases, focus, modifiers, trigger changes, session transitions, permission loss, and legacy-app collision.
- `tests/protocol_test.py`: 17 native process checks, including validation, duplicate-helper rejection, EOF, SIGTERM, and parent exit while stdin remains open. The script never enables capture or requests permissions.
- `tests/memory-check.json`: 100,000 simulated pulse pairs through the production adapter, with zero actual posted events. RSS was 15,840KB at 1,000 pulses, 16,016KB at 10,000, and 16,128KB at 100,000.

These checks do not establish real-game damage registration, permission-granted execution of the newly bundled helper, or long-session game behavior. Those require verification in the target game.
