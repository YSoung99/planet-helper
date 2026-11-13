import Foundation

struct RepeatState {
    private(set) var enabled = false
    private(set) var held = false
    private(set) var requiresRelease = false
    private(set) var repeating = false
    private(set) var generation = 0

    mutating func enable(keyAlreadyHeld: Bool) {
        enabled = true
        held = keyAlreadyHeld
        requiresRelease = keyAlreadyHeld
        repeating = false
        generation += 1
    }

    mutating func disable() {
        enabled = false
        held = false
        requiresRelease = false
        repeating = false
        generation += 1
    }

    mutating func interrupt() {
        repeating = false
        requiresRelease = held
        generation += 1
    }

    mutating func keyUp() {
        held = false
        requiresRelease = false
        repeating = false
        generation += 1
    }

    // Returning to the game or releasing a modifier never resumes an old hold.
    mutating func keyDown(autorepeat: Bool, targetFrontmost: Bool, modifiers: Bool) -> Bool {
        guard enabled, !autorepeat, !held else { return false }
        held = true
        guard !requiresRelease, targetFrontmost, !modifiers else {
            requiresRelease = true
            return false
        }
        repeating = true
        generation += 1
        return true
    }

    func canSend(generation token: Int, targetFrontmost: Bool, modifiers: Bool) -> Bool {
        enabled && held && repeating && !requiresRelease && generation == token && targetFrontmost && !modifiers
    }
}

struct RepeatSettings {
    var initialDelay: Int = 230
    var minimumInterval: Int = 85
    var maximumInterval: Int = 120
    // Physical trigger: A (0), with optional explicit Z (6) selection. Output remains A.
    var attackTriggerKeyCode: Int = 0

    var valid: Bool {
        (100...2000).contains(initialDelay) && (50...1000).contains(minimumInterval)
        && (50...1000).contains(maximumInterval) && minimumInterval <= maximumInterval
        && [0, 6].contains(attackTriggerKeyCode)
    }

    static func load(from defaults: UserDefaults = .standard) -> RepeatSettings {
        let value = RepeatSettings(
            initialDelay: defaults.object(forKey: "initialDelayMS") as? Int ?? 230,
            minimumInterval: defaults.object(forKey: "minimumIntervalMS") as? Int ?? 85,
            maximumInterval: defaults.object(forKey: "maximumIntervalMS") as? Int ?? 120,
            attackTriggerKeyCode: defaults.object(forKey: "attackTriggerKeyCode") as? Int ?? 0)
        return value.valid ? value : RepeatSettings()
    }

    func save(to defaults: UserDefaults = .standard) {
        guard valid else { return }
        defaults.set(initialDelay, forKey: "initialDelayMS")
        defaults.set(minimumInterval, forKey: "minimumIntervalMS")
        defaults.set(maximumInterval, forKey: "maximumIntervalMS")
        defaults.set(attackTriggerKeyCode, forKey: "attackTriggerKeyCode")
    }
}
