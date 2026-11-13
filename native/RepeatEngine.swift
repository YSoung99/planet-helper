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
        Permissions(listen: CGPreflightListenEventAccess(), post: CGPreflightPostEventAccess())
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
        NSWorkspace.shared.frontmostApplication?.bundleIdentifier == targetBundleID
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
            if resume { start() }
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
        down.post(tap: .cghidEventTap)
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
            up.post(tap: .cghidEventTap)
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

