import AppKit
import ApplicationServices
import CoreGraphics
import Darwin
import Foundation

// The legacy app owns its event tap independently; never run both engines.
func legacyRepeaterIsRunning() -> Bool {
    !NSRunningApplication.runningApplications(withBundleIdentifier: "local.codex.mapleworldkeyrepeat").isEmpty
}

// A stalled parent must not block the event-tap callback or queue unbounded data.
// Only the newest status snapshot is retained while a previous write completes.
final class StatusWriter {
    private let lock = NSLock()
    private var latest: Data?
    private var writing = false
    private let queue = DispatchQueue(label: "local.codex.planet.repeat.status")
    func send(_ object: [String: Any]) {
        guard let data = try? JSONSerialization.data(withJSONObject: object, options: [.sortedKeys]),
              var text = String(data: data, encoding: .utf8) else { return }
        text += "\n"
        lock.lock()
        latest = Data(text.utf8)
        if writing { lock.unlock(); return }
        writing = true
        lock.unlock()
        queue.async { [self] in
            while true {
                lock.lock()
                guard let data = latest else { writing = false; lock.unlock(); return }
                latest = nil
                lock.unlock()
                do { try FileHandle.standardOutput.write(contentsOf: data) }
                catch { DispatchQueue.main.async { finish() }; return }
            }
        }
    }
}

let writer = StatusWriter()
let repeater = KeyRepeater()
let parentPID = getppid()
var exiting = false
var handlingCommand = false
var protocolError = ""
var requestID: Any?
var lockFD: Int32 = -1
var lastSnapshot = ""
var signalSources: [DispatchSourceSignal] = []

func statusSnapshot() -> [String: Any] {
    let permissions = Permissions.current()
    var snapshot: [String: Any] = [
        "type": "status", "protocolVersion": 1,
        "enabled": repeater.isRunning, "active": repeater.active,
        "accessibility": permissions.post, "inputMonitoring": permissions.listen,
        "postPermissionName": postPermissionName,
        "legacyRunning": legacyRepeaterIsRunning(),
        "status": repeater.message, "error": protocolError,
        "settings": ["firstDelayMs": repeater.settings.initialDelay,
                     "minIntervalMs": repeater.settings.minimumInterval,
                     "maxIntervalMs": repeater.settings.maximumInterval,
                     "attackTriggerKeyCode": repeater.settings.attackTriggerKeyCode]
    ]
    if let requestID { snapshot["requestId"] = requestID }
    return snapshot
}
func emitStatus(force: Bool = true) {
    let snapshot = statusSnapshot()
    let fingerprint = String(data: (try? JSONSerialization.data(withJSONObject: snapshot, options: [.sortedKeys])) ?? Data(), encoding: .utf8) ?? ""
    if force || fingerprint != lastSnapshot {
        lastSnapshot = fingerprint
        writer.send(snapshot)
    }
}
func finish() {
    guard !exiting else { return }
    exiting = true
    repeater.stop()
    if lockFD >= 0 { _ = flock(lockFD, LOCK_UN); close(lockFD) }
    exit(0)
}
func integer(_ value: Any?) -> Int? {
    guard let value = value as? NSNumber,
          CFGetTypeID(value) != CFBooleanGetTypeID(),
          value.doubleValue.isFinite,
          value.doubleValue.rounded() == value.doubleValue,
          value.doubleValue >= Double(Int.min), value.doubleValue < Double(Int.max) else { return nil }
    return value.intValue
}
func handle(_ line: String) {
    handlingCommand = true
    defer {
        if !protocolError.isEmpty && repeater.isRunning { repeater.stop(message: protocolError) }
        handlingCommand = false
        emitStatus()
    }
    protocolError = ""
    requestID = nil
    guard let data = line.data(using: .utf8),
          let command = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
          let action = command["command"] as? String else {
        protocolError = "잘못된 제어 요청이에요."
        return
    }
    if let id = command["requestId"], id is String || id is NSNumber { requestID = id }
    switch action {
    case "status": break
    case "configure":
        var settings = repeater.settings
        let fields: [(String, (inout RepeatSettings, Int) -> Void)] = [
            ("firstDelayMs", { $0.initialDelay = $1 }),
            ("minIntervalMs", { $0.minimumInterval = $1 }),
            ("maxIntervalMs", { $0.maximumInterval = $1 }),
            ("attackTriggerKeyCode", { $0.attackTriggerKeyCode = $1 })
        ]
        for (key, assign) in fields where command[key] != nil {
            guard let value = integer(command[key]) else {
                protocolError = "반복 설정에는 정수를 입력해 주세요."
                return
            }
            assign(&settings, value)
        }
        guard settings.valid else {
            protocolError = "반복 간격과 키 설정을 확인해 주세요."
            return
        }
        if let enabled = command["enabled"], !(enabled is NSNumber && CFGetTypeID(enabled as CFTypeRef) == CFBooleanGetTypeID()) {
            protocolError = "켜짐 설정을 확인해 주세요."
            return
        }
        if command["enabled"] as? Bool == false { repeater.stop() }
        repeater.applySettings(settings)
        if command["enabled"] as? Bool == true {
            repeater.start()
            if !repeater.isRunning { protocolError = repeater.message }
        }
    case "request_permissions":
        // Prompts happen only on an explicit command from the unified UI.
        _ = CGRequestListenEventAccess()
        _ = CGRequestPostEventAccess()
        repeater.refreshPermissions()
    case "shutdown": finish()
    default: protocolError = "지원하지 않는 제어 요청이에요."
    }
}

signal(SIGPIPE, SIG_IGN)
for number in [SIGTERM, SIGINT, SIGHUP] {
    signal(number, SIG_IGN)
    let source = DispatchSource.makeSignalSource(signal: number, queue: .main)
    source.setEventHandler { finish() }
    source.resume()
    signalSources.append(source)
}

let lockDirectory: URL = {
    if let override = ProcessInfo.processInfo.environment["PLANET_EXP_DATA_DIR"], !override.isEmpty {
        return URL(fileURLWithPath: override, isDirectory: true)
    }
    return FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent("Library/Application Support/Maple Planet Helper", isDirectory: true)
}()
do {
    try FileManager.default.createDirectory(at: lockDirectory, withIntermediateDirectories: true)
    lockFD = open(lockDirectory.appendingPathComponent("repeat-helper.lock").path, O_CREAT | O_RDWR, S_IRUSR | S_IWUSR)
    guard lockFD >= 0, flock(lockFD, LOCK_EX | LOCK_NB) == 0 else {
        protocolError = "다른 통합 앱이 키 반복을 사용하고 있어요."
        // No input has been captured, so synchronous output is safe on this exit path.
        let data = try JSONSerialization.data(withJSONObject: statusSnapshot(), options: [.sortedKeys])
        try FileHandle.standardOutput.write(contentsOf: data + Data([10]))
        exit(2)
    }
} catch {
    protocolError = "키 반복 준비 파일을 만들지 못했어요."
    let data = (try? JSONSerialization.data(withJSONObject: statusSnapshot())) ?? Data()
    try? FileHandle.standardOutput.write(contentsOf: data + Data([10]))
    exit(2)
}

_ = NSApplication.shared
NSApp.setActivationPolicy(.prohibited)
repeater.onChange = { if !exiting && !handlingCommand { emitStatus(force: false) } }
emitStatus()
let watchdog = DispatchSource.makeTimerSource(queue: .main)
watchdog.schedule(deadline: .now() + .milliseconds(500), repeating: .milliseconds(500))
watchdog.setEventHandler {
    if getppid() != parentPID || parentPID <= 1 { finish() }
    if repeater.isRunning && legacyRepeaterIsRunning() {
        repeater.stop(message: "기존 키 반복 앱이 실행되어 기능을 껐어요.")
    }
    emitStatus(force: false)
}
watchdog.resume()
DispatchQueue.global(qos: .utility).async {
    while let line = readLine() {
        DispatchQueue.main.async { handle(line) }
    }
    DispatchQueue.main.async { finish() }
}
RunLoop.main.run()
