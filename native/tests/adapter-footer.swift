setbuf(stdout, nil)
var testTarget = true
var testLegacyRunning = false
var testListen = true
var testPost = true
var storeEvents = true
var totalPosted = 0
struct Output { let key: CGKeyCode; let down: Bool; let time: TimeInterval }
var outputs: [Output] = []
var checks = 0
func check(_ value: Bool, _ name: String) {
    precondition(value, name)
    checks += 1
    print("PASS: \(name)")
}
func runFor(_ seconds: TimeInterval) { RunLoop.main.run(until: Date().addingTimeInterval(seconds)) }
func recordEvent(_ event: CGEvent) {
    precondition(event.flags.isEmpty)
    precondition(event.getIntegerValueField(.eventSourceUserData) == syntheticMarker)
    precondition(event.getIntegerValueField(.keyboardEventAutorepeat) == 0)
    precondition(!repeater.testHandle(event.type, event: event), "synthetic events must pass the filter")
    totalPosted += 1
    if storeEvents {
        outputs.append(Output(key: CGKeyCode(event.getIntegerValueField(.keyboardEventKeycode)),
                              down: event.type == .keyDown, time: ProcessInfo.processInfo.systemUptime))
    }
}
extension KeyRepeater {
    func testEnable(held: Bool = false, trigger: Int = 0) {
        isRunning = true
        modifiersDown = false
        settings = RepeatSettings(attackTriggerKeyCode: trigger)
        for key in keys { states[key]?.enable(keyAlreadyHeld: held) }
    }
    func testHandle(_ type: CGEventType, event: CGEvent) -> Bool { handle(type: type, event: event) }
    func testEvent(_ type: CGEventType, key: CGKeyCode = 0, flags: CGEventFlags = [], autorepeat: Bool = false,
                   synthetic: Bool = false) -> Bool {
        let event = CGEvent(keyboardEventSource: nil, virtualKey: key, keyDown: type != .keyUp)!
        event.flags = flags
        event.setIntegerValueField(.keyboardEventAutorepeat, value: autorepeat ? 1 : 0)
        if synthetic { event.setIntegerValueField(.eventSourceUserData, value: syntheticMarker) }
        return handle(type: type, event: event)
    }
    func testFocus() { interrupt(); showStatus() }
    func testSend(_ key: CGKeyCode) -> Bool { sendOnePress(for: key, token: states[key]!.generation) }
    func memoryPress(_ key: CGKeyCode) {
        if states[key]?.held != true {
            _ = states[key]?.keyDown(autorepeat: false, targetFrontmost: true, modifiers: false)
        }
        precondition(testSend(key))
        releaseKey(key)
    }
    var testPending: Bool { !pendingUps.isEmpty || !releaseTimers.isEmpty || !timers.isEmpty }
    func testActive(_ key: CGKeyCode) -> Bool { states[key]!.repeating }
    func testResumeAfterSettings() {
        isRunning = true
        for key in keys { states[key]?.enable(keyAlreadyHeld: false) }
    }
}
let repeater = KeyRepeater()
func arm(held: Bool = false) {
    repeater.stop()
    outputs.removeAll(keepingCapacity: true)
    testTarget = true; testListen = true; testPost = true
    repeater.testEnable(held: held)
}

if CommandLine.arguments.contains("--memory") {
    storeEvents = false
    arm()
    var done = 0
    for target in [1000, 10000, 100000] {
        for i in done..<target {
            repeater.memoryPress([0, 36, 76][i % 3])
            // Canceled dispatch sources finish cleanup on the main queue.
            // Let that queue run as it does between real game input pulses.
            if i % 1000 == 999 { runFor(0.003) }
        }
        done = target
        precondition(totalPosted == target * 2)
        precondition(!repeater.testPending)
        print("READY \(target) \(getpid())")
        precondition(readLine() == "continue")
    }
    repeater.stop()
    print("PASS: 100000 production pulses with immediate test-only cleanup; no events posted to other apps")
    exit(0)
}

arm()
check(repeater.testEvent(.keyDown), "physical A down is suppressed")
check(outputs.count == 1 && outputs[0].down, "first synthetic down is immediate")
check(repeater.testEvent(.keyDown, autorepeat: true) && outputs.count == 1, "physical autorepeat is suppressed without an extra down")
check(!repeater.testEvent(.keyUp, synthetic: true) && repeater.testActive(0), "synthetic keyUp passes without stopping physical hold")
check(!repeater.testEvent(.keyDown, key: 123, flags: .maskSecondaryFn), "arrow key passes through")
runFor(0.075)
check(outputs.count == 2 && !outputs[1].down, "pulse releases independently before the next press")
check(outputs[1].time - outputs[0].time >= 0.035, "default down duration is at least 35ms")
runFor(0.24)
check(outputs.count >= 4, "first repeated pulse arrives after the initial delay")
check(outputs[2].time - outputs[0].time >= 0.225, "230ms initial delay is preserved")
check(outputs[2].time - outputs[1].time >= 0.18, "initial released interval exists")
runFor(0.7)
let natural = outputs
var downTimes: [TimeInterval] = []
for (i, output) in natural.enumerated() {
    check(output.down == (i % 2 == 0), "output \(i) strictly alternates down/up")
    if output.down { downTimes.append(output.time) }
    if i > 0 && !output.down { check(output.time - natural[i-1].time >= 0.035, "pulse \(i/2) has a visible down interval") }
    if i > 2 && output.down { check(output.time - natural[i-1].time >= 0.040, "pulse \(i/2) has a separate up interval") }
}
check(repeater.testEvent(.keyUp), "physical A up is suppressed after capture")
let stoppedCount = outputs.count
check(!repeater.testPending && !outputs.last!.down, "release leaves no held synthetic key or timers")
runFor(0.25)
check(outputs.count == stoppedCount, "release cannot produce another pulse")

arm()
_ = repeater.testEvent(.keyDown)
check(repeater.testEvent(.keyUp), "very short physical tap is captured")
check(outputs.count == 2 && outputs[0].down && !outputs[1].down, "short tap still gets one complete synthetic press")
runFor(0.3)
check(outputs.count == 2, "short tap has no delayed duplicate")

arm()
testTarget = false
check(!repeater.testEvent(.keyDown) && outputs.isEmpty, "A in another app passes unchanged")
testTarget = true
repeater.testFocus()
check(!repeater.testEvent(.keyDown, autorepeat: true) && outputs.isEmpty, "held outside cannot start on focus return")
check(!repeater.testEvent(.keyUp), "uncaptured physical up passes through")
check(repeater.testEvent(.keyDown), "fresh press after focus return starts")
testTarget = false
repeater.testFocus()
check(!repeater.testPending && !outputs.last!.down, "focus loss releases the current pulse")
check(repeater.testEvent(.keyDown, autorepeat: true), "captured hardware repeat cannot leak after focus loss")
check(repeater.testEvent(.keyUp), "captured hardware up is consumed after focus loss")

arm()
check(!repeater.testEvent(.keyDown, flags: .maskCommand) && outputs.isEmpty, "Command+A passes without conversion")
_ = repeater.testEvent(.keyUp)
_ = repeater.testEvent(.keyDown)
_ = repeater.testEvent(.flagsChanged, key: 55, flags: .maskCommand)
check(!repeater.testPending && !outputs.last!.down, "modifier press releases pending synthetic down")
_ = repeater.testEvent(.flagsChanged, key: 55)
check(!repeater.testActive(0), "modifier release cannot resume the old hold")
_ = repeater.testEvent(.keyUp)

arm()
check(!repeater.testEvent(.keyDown, key: 1) && outputs.isEmpty, "unrelated key passes through")
for key: CGKeyCode in [0, 36, 76] { check(repeater.testEvent(.keyDown, key: key), "key \(key) starts its own pulse") }
check(Set(outputs.filter(\.down).map(\.key)) == [0, 36, 76], "A/Return/keypad Enter remain independent")
_ = repeater.testEvent(.keyUp)
check(!repeater.testActive(0) && repeater.testActive(36) && repeater.testActive(76), "A release leaves both Enter states running")
repeater.stop()
check(!repeater.testPending && outputs.filter(\.down).count == outputs.filter({ !$0.down }).count, "stop releases every synthetic key")

arm(held: true)
check(!repeater.testEvent(.keyDown, autorepeat: true) && outputs.isEmpty, "held at startup requires physical release")
_ = repeater.testEvent(.keyUp)
check(repeater.testEvent(.keyDown), "fresh press after startup release is converted")
testPost = false
check(!repeater.testSend(0) && !repeater.isRunning && !repeater.testPending, "permission loss stops and cleans up")

arm()
_ = repeater.testEvent(.keyDown)
_ = repeater.testEvent(.tapDisabledByTimeout)
runFor(0.01)
check(!repeater.isRunning && !repeater.testPending, "event tap timeout fails closed and releases keys")
for name in [NSWorkspace.willSleepNotification, NSWorkspace.screensDidSleepNotification, NSWorkspace.sessionDidResignActiveNotification] {
    arm()
    _ = repeater.testEvent(.keyDown)
    NSWorkspace.shared.notificationCenter.post(name: name, object: nil)
    runFor(0.01)
    check(!repeater.isRunning && !repeater.testPending, "\(name.rawValue) releases and stops")
}
arm()
_ = repeater.testEvent(.keyDown)
repeater.settingsChanged()
check(!repeater.testPending && !repeater.testActive(0), "settings change interrupts pending pulse")
_ = repeater.testEvent(.keyUp)
_ = repeater.testEvent(.keyDown)
let before = outputs.count
_ = repeater.testEvent(.keyUp)
_ = repeater.testEvent(.keyDown)
runFor(0.075)
check(outputs.count == before + 3 && !outputs.last!.down, "canceled release cannot disturb a new key generation")
repeater.stop()
arm()
_ = repeater.testEvent(.keyDown)
repeater.applySettings(RepeatSettings(attackTriggerKeyCode: 6))
check(repeater.isRunning && !repeater.testPending && !outputs.last!.down,
      "switch to Z releases pending A and resumes without stale timers")
let switchCount = outputs.count
runFor(0.28)
check(outputs.count == switchCount, "old A hold cannot leak into Z mode")
check(!repeater.testEvent(.keyDown, key: 0) && outputs.count == switchCount,
      "physical A passes untouched in Z mode")
check(repeater.testEvent(.keyDown, key: 6) && outputs.last!.key == 0 && outputs.last!.down,
      "Z mode creates A down")
runFor(0.075)
check(outputs.last!.key == 0 && !outputs.last!.down, "Z mode creates timed A up")
runFor(0.25)
check(outputs.count > switchCount + 2 && outputs.dropFirst(switchCount).allSatisfy { $0.key == 0 },
      "holding Z repeats A only")
check(repeater.testEvent(.keyDown, key: 6, autorepeat: true), "Z hardware repeat stays suppressed")
_ = repeater.testEvent(.keyDown, key: 36)
_ = repeater.testEvent(.keyDown, key: 76)
_ = repeater.testEvent(.keyUp, key: 6)
check(!repeater.testActive(6) && repeater.testActive(36) && repeater.testActive(76),
      "Z release leaves both Enter keys independent")
repeater.stop()
check(outputs.filter(\.down).count == outputs.filter({ !$0.down }).count,
      "Z and Enter output pairs balance on stop")
repeater.applySettings(RepeatSettings(attackTriggerKeyCode: 0))
check(!repeater.isRunning && repeater.settings.attackTriggerKeyCode == 0,
      "changing trigger while stopped does not start the feature")
repeater.testEnable(trigger: 6)
_ = repeater.testEvent(.keyDown, key: 6)
repeater.applySettings(RepeatSettings(attackTriggerKeyCode: 0))
check(!repeater.testActive(6) && !repeater.testPending && !outputs.last!.down,
      "switch back to A releases pending Z output")
check(repeater.testEvent(.keyDown, key: 0), "legacy A mode starts after switching back")
repeater.stop()
arm()
_ = repeater.testEvent(.keyDown)
testLegacyRunning = true
check(!repeater.testSend(0) && !repeater.isRunning && !repeater.testPending,
      "legacy app launch stops the active pulse and releases output")
testLegacyRunning = false
repeater.stop()
print("\(checks) adapter checks passed; no actual posting or event tap creation.")
