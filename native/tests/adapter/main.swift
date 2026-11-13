import AppKit
import ApplicationServices
import CoreGraphics

private let targetBundleID = "com.nexon.mod.inhouse"
private let syntheticMarker: Int64 = 0x4D575241
private let blockedModifiers: CGEventFlags = [.maskCommand, .maskControl, .maskAlternate, .maskShift]
var postPermissionName: String {
    if #available(macOS 27.0, *) { return "기기 제어" }
    return "손쉬운 사용"
}

struct Permissions {
    var listen: Bool
    var post: Bool
    var ready: Bool { listen && post }
    var summary: String {
        "입력 모니터링: \(listen ? "허용" : "필요") · \(postPermissionName): \(post ? "허용" : "필요")"
    }
    static func current() -> Permissions {
        Permissions(listen: testListen, post: testPost)
    }
}

final class KeyRepeater {
    var onChange: (() -> Void)?
    private(set) var settings = RepeatSettings.load(from: UserDefaults(suiteName: "local.codex.mapleworldkeyrepeat")!)
    private(set) var message = "기능 꺼짐"
    private var keys: [CGKeyCode] { [CGKeyCode(settings.attackTriggerKeyCode), 36, 76] }
    private var attackKeyName: String { settings.attackTriggerKeyCode == 6 ? "Z → A" : "A" }
    // A private source owns shared state on macOS. Keep one for the app's
    // lifetime so repeated presses cannot accumulate shared memory mappings.
    private let eventSource = CGEventSource(stateID: .privateState)
    private var states: [CGKeyCode: RepeatState] = [0: RepeatState(), 6: RepeatState(), 36: RepeatState(), 76: RepeatState()]
    private(set) var isRunning = false
    var active: Bool { isRunning && states.values.contains(where: { $0.repeating }) }
    private var tap: CFMachPort?
    private var source: CFRunLoopSource?
    private var timers: [CGKeyCode: DispatchSourceTimer] = [:]
    private var releaseTimers: [CGKeyCode: DispatchSourceTimer] = [:]
    private var pendingUps: [CGKeyCode: CGEvent] = [:]
    private var capturedKeys: Set<CGKeyCode> = []
    // Leave a real down interval and a separate up interval, even at the
    // smallest supported cycle. The default is 40ms down, 45–80ms up.
    private var pressDuration: Int { min(40, settings.minimumInterval / 2) }
    private var permissionTimer: DispatchSourceTimer?
    private var modifiersDown = false
    private var observers: [NSObjectProtocol] = []
    private var lockObserver: NSObjectProtocol?
    private var isTargetFrontmost: Bool {
        testTarget
    }

    init() {
        let center = NSWorkspace.shared.notificationCenter
        observers.append(center.addObserver(forName: NSWorkspace.didActivateApplicationNotification,
                                            object: nil, queue: .main) { [weak self] _ in
            guard let self, self.isRunning else { return }
            self.interrupt()
            self.showStatus()
        })
        for name in [NSWorkspace.willSleepNotification, NSWorkspace.screensDidSleepNotification,
                     NSWorkspace.sessionDidResignActiveNotification] {
            observers.append(center.addObserver(forName: name, object: nil, queue: .main) { [weak self] _ in
                self?.stop(message: "잠자기·세션 변경으로 기능을 껐어요. 다시 시작해 주세요.")
            })
        }
        // Supplemental lock notification; display sleep/session changes use public APIs above.
        lockObserver = DistributedNotificationCenter.default().addObserver(
            forName: NSNotification.Name("com.apple.screenIsLocked"), object: nil, queue: .main) { [weak self] _ in
                self?.stop(message: "화면 잠금으로 기능을 껐어요. 다시 시작해 주세요.")
            }
    }
    deinit {
        observers.forEach { NSWorkspace.shared.notificationCenter.removeObserver($0) }
        if let lockObserver { DistributedNotificationCenter.default().removeObserver(lockObserver) }
    }
    private func update(_ text: String) {
        message = text
        onChange?()
    }
    func refreshPermissions() {
        if !Permissions.current().ready {
            stop(message: "권한이 필요해요. 아래 권한 상태를 확인해 주세요.")
        } else if isRunning { showStatus() }
        else { update("권한 준비 완료 · 시작 버튼을 누르세요.") }
    }
    func start() {
        guard !isRunning else { return }
        guard !legacyRepeaterIsRunning() else {
            update("기존 키 반복 앱을 종료한 뒤 켜 주세요.")
            return
        }
        guard Permissions.current().ready else {
            update("권한이 필요해요. 설정에서 허용 후 ‘권한 다시 확인’을 누르세요.")
            return
        }
        let types: [CGEventType] = [.keyDown, .keyUp, .flagsChanged]
        let mask = types.reduce(CGEventMask(0)) { $0 | (CGEventMask(1) << $1.rawValue) }
        let callback: CGEventTapCallBack = { _, type, event, userInfo in
            guard let userInfo else { return Unmanaged.passUnretained(event) }
            let suppress = Unmanaged<KeyRepeater>.fromOpaque(userInfo).takeUnretainedValue().handle(type: type, event: event)
            return suppress ? nil : Unmanaged.passUnretained(event)
        }
        guard let port = CGEvent.tapCreate(tap: .cgSessionEventTap, place: .headInsertEventTap,
            options: .defaultTap, eventsOfInterest: mask, callback: callback,
            userInfo: Unmanaged.passUnretained(self).toOpaque()) else {
            update("키 감시를 시작하지 못했어요. 권한을 확인하고 앱을 다시 열어 주세요.")
            return
        }
        guard let runLoopSource = CFMachPortCreateRunLoopSource(kCFAllocatorDefault, port, 0) else {
            CFMachPortInvalidate(port)
            update("키 감시를 준비하지 못했어요. 앱을 다시 열어 주세요.")
            return
        }
        tap = port
        source = runLoopSource
        CFRunLoopAddSource(CFRunLoopGetMain(), runLoopSource, .commonModes)
        for key in keys {
            states[key]?.enable(keyAlreadyHeld: CGEventSource.keyState(.combinedSessionState, key: key))
        }
        isRunning = true
        modifiersDown = !CGEventSource.flagsState(.combinedSessionState).intersection(blockedModifiers).isEmpty
        CGEvent.tapEnable(tap: port, enable: true)
        let check = DispatchSource.makeTimerSource(queue: .main)
        check.schedule(deadline: .now() + .seconds(1), repeating: .seconds(1))
        check.setEventHandler { [weak self] in
            if legacyRepeaterIsRunning() { self?.stop(message: "기존 키 반복 앱이 실행되어 기능을 껐어요.") }
            else if !Permissions.current().ready { self?.stop(message: "권한이 해제되어 기능을 껐어요.") }
        }
        permissionTimer = check
        check.resume()
        showStatus()
    }
    func stop(message: String = "기능 꺼짐") {
        cancelAllTimers()
        permissionTimer?.cancel()
        permissionTimer = nil
        for key in keys { states[key]?.disable() }
        isRunning = false
        if let tap { CGEvent.tapEnable(tap: tap, enable: false) }
        if let source { CFRunLoopRemoveSource(CFRunLoopGetMain(), source, .commonModes) }
        if let tap { CFMachPortInvalidate(tap) }
        source = nil
        tap = nil
        capturedKeys.removeAll()
        update(message)
    }
    func settingsChanged() {
        interrupt()
        if isRunning { showStatus() }
    }
    func applySettings(_ value: RepeatSettings) {
        guard value.valid else { return }
        let changesTrigger = settings.attackTriggerKeyCode != value.attackTriggerKeyCode
        let resume = isRunning
        // Clear the old trigger and pending output before switching keys.
        if changesTrigger { stop() }
        settings = value
        if changesTrigger {
            if resume { testResumeAfterSettings() }
        } else { settingsChanged() }
    }
    private func interrupt() {
        cancelAllTimers()
        for key in keys { states[key]?.interrupt() }
    }
    private func showStatus() {
        guard isRunning else { return }
        let active = keys.filter { states[$0]?.repeating == true }
        if !isTargetFrontmost {
            update("대기 중 · 메이플스토리 월드를 앞으로 열어 주세요.")
        } else if !active.isEmpty {
            var names: [String] = []
            if active.contains(CGKeyCode(settings.attackTriggerKeyCode)) { names.append(attackKeyName) }
            if active.contains(36) || active.contains(76) { names.append("Enter") }
            update("반복 중 · \(names.joined(separator: " + ")) · 키를 떼면 멈춰요.")
        } else if states.values.contains(where: { $0.requiresRelease }) {
            update("대기 중 · 누른 키를 떼었다가 다시 누르세요.")
        } else if modifiersDown {
            update("대기 중 · 보조키를 떼고 \(attackKeyName) 또는 Enter를 누르세요.")
        } else {
            update("준비 완료 · \(attackKeyName) 또는 Enter를 누르는 동안 반복해요.")
        }
    }
    // true means the active event tap must discard this physical event.
    // Marked synthetic events always pass through, including their keyUps.
    private func handle(type: CGEventType, event: CGEvent) -> Bool {
        if type == .tapDisabledByTimeout || type == .tapDisabledByUserInput {
            interrupt()
            DispatchQueue.main.async { [weak self] in
                self?.stop(message: "키 감시가 끊겨 기능을 껐어요. 키를 떼고 다시 시작해 주세요.")
            }
            return false
        }
        guard isRunning, event.getIntegerValueField(.eventSourceUserData) != syntheticMarker else { return false }
        let flags = event.flags
        if type == .flagsChanged {
            modifiersDown = !flags.intersection(blockedModifiers).isEmpty
            if modifiersDown { interrupt() }
            showStatus()
            return false
        }
        let rawKey = event.getIntegerValueField(.keyboardEventKeycode)
        guard (0...Int64(UInt16.max)).contains(rawKey) else { return false }
        let key = CGKeyCode(rawKey)
        guard keys.contains(key) else { return false }
        modifiersDown = !flags.intersection(blockedModifiers).isEmpty
        if type == .keyUp {
            let captured = capturedKeys.remove(key) != nil
            cancelTimer(for: key)
            releaseKey(key)
            states[key]?.keyUp()
            showStatus()
            return captured
        } else if type == .keyDown {
            // Swallow hardware autorepeat and duplicate down events until
            // physical release, including after a focus/modifier interrupt.
            if capturedKeys.contains(key) { return true }
            let began = states[key]?.keyDown(autorepeat: event.getIntegerValueField(.keyboardEventAutorepeat) != 0,
                targetFrontmost: isTargetFrontmost, modifiers: modifiersDown) ?? false
            if began, let token = states[key]?.generation {
                capturedKeys.insert(key)
                // Emit the first down before returning so even a short tap
                // has a matching press. Release is asynchronous; never sleep
                // or spin inside the event-tap callback.
                _ = sendOnePress(for: key, token: token, nextInterval: settings.initialDelay)
            }
            showStatus()
            return began
        }
        return false
    }
    private func scheduleNextPress(for key: CGKeyCode, after milliseconds: Int, token: Int) {
        cancelTimer(for: key)
        let next = DispatchSource.makeTimerSource(queue: .main)
        next.schedule(deadline: .now() + .milliseconds(milliseconds))
        next.setEventHandler { [weak self] in
            guard let self, self.states[key]?.generation == token else { return }
            self.timers[key] = nil
            _ = self.sendOnePress(for: key, token: token)
        }
        timers[key] = next
        next.resume()
    }
    private func sendOnePress(for key: CGKeyCode, token: Int, nextInterval: Int? = nil) -> Bool {
        guard states[key]?.canSend(generation: token, targetFrontmost: isTargetFrontmost, modifiers: modifiersDown) == true else {
            interrupt()
            showStatus()
            return false
        }
        guard !legacyRepeaterIsRunning() else {
            stop(message: "기존 키 반복 앱이 실행되어 기능을 껐어요.")
            return false
        }
        guard Permissions.current().ready else {
            stop(message: "권한이 해제되어 기능을 껐어요.")
            return false
        }
        guard pendingUps[key] == nil else { return false }
        let outputKey: CGKeyCode = key == CGKeyCode(settings.attackTriggerKeyCode) ? 0 : key
        guard let eventSource,
              let down = CGEvent(keyboardEventSource: eventSource, virtualKey: outputKey, keyDown: true),
              let up = CGEvent(keyboardEventSource: eventSource, virtualKey: outputKey, keyDown: false) else {
            stop(message: "키 입력 신호를 만들지 못해 기능을 껐어요.")
            return false
        }
        for event in [down, up] {
            event.flags = []
            event.setIntegerValueField(.eventSourceUserData, value: syntheticMarker)
            event.setIntegerValueField(.keyboardEventAutorepeat, value: 0)
        }
        recordEvent(down)
        pendingUps[key] = up
        let duration = pressDuration
        let interval = nextInterval ?? Int.random(in: settings.minimumInterval...settings.maximumInterval)
        let release = DispatchSource.makeTimerSource(queue: .main)
        release.schedule(deadline: .now() + .milliseconds(duration))
        release.setEventHandler { [weak self] in
            guard let self, self.states[key]?.generation == token else { return }
            self.releaseKey(key)
            guard self.states[key]?.canSend(generation: token, targetFrontmost: self.isTargetFrontmost,
                                           modifiers: self.modifiersDown) == true else {
                self.interrupt()
                self.showStatus()
                return
            }
            self.scheduleNextPress(for: key, after: max(20, interval - duration), token: token)
        }
        releaseTimers[key] = release
        release.resume()
        return true
    }
    private func releaseKey(_ key: CGKeyCode) {
        releaseTimers[key]?.cancel()
        releaseTimers[key] = nil
        if let up = pendingUps.removeValue(forKey: key) {
            recordEvent(up)
        }
    }
    private func cancelTimer(for key: CGKeyCode) {
        timers[key]?.cancel()
        timers[key] = nil
    }
    private func cancelAllTimers() {
        for timer in timers.values { timer.cancel() }
        timers.removeAll()
        for key in Array(pendingUps.keys) { releaseKey(key) }
    }
}


func legacyRepeaterIsRunning() -> Bool { testLegacyRunning }
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
