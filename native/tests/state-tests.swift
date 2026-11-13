import Foundation

@main struct Tests {
    static func main() {
        var checks = 0
        func check(_ pass: Bool, _ name: String) {
            precondition(pass, name)
            checks += 1
            print("PASS: \(name)")
        }
        var state = RepeatState()
        check(!state.keyDown(autorepeat: false, targetFrontmost: true, modifiers: false), "disabled input")
        state.enable(keyAlreadyHeld: false)
        check(state.keyDown(autorepeat: false, targetFrontmost: true, modifiers: false), "fresh A begins")
        let token = state.generation
        check(state.canSend(generation: token, targetFrontmost: true, modifiers: false), "valid timer sends")
        state.keyUp()
        check(!state.canSend(generation: token, targetFrontmost: true, modifiers: false), "release invalidates queued timer")
        check(!state.keyDown(autorepeat: true, targetFrontmost: true, modifiers: false), "system autorepeat ignored")
        check(!state.keyDown(autorepeat: false, targetFrontmost: true, modifiers: true), "Command+A blocked")
        state.keyUp()
        _ = state.keyDown(autorepeat: false, targetFrontmost: true, modifiers: false)
        let modifierToken = state.generation
        state.interrupt()
        check(!state.canSend(generation: modifierToken, targetFrontmost: true, modifiers: false), "modifier release cannot resume old hold")
        state.keyUp()
        _ = state.keyDown(autorepeat: false, targetFrontmost: true, modifiers: false)
        let focusToken = state.generation
        state.interrupt()
        check(!state.canSend(generation: focusToken, targetFrontmost: true, modifiers: false), "quick app switch invalidates old hold")
        state.keyUp()
        check(state.keyDown(autorepeat: false, targetFrontmost: true, modifiers: false), "fresh press after focus change")
        state.keyUp()
        check(!state.keyDown(autorepeat: false, targetFrontmost: false, modifiers: false), "press in other app blocked")
        check(!state.keyDown(autorepeat: true, targetFrontmost: true, modifiers: false), "held outside cannot start when returning")
        state.keyUp()
        _ = state.keyDown(autorepeat: false, targetFrontmost: true, modifiers: false)
        let stopToken = state.generation
        state.disable()
        check(!state.canSend(generation: stopToken, targetFrontmost: true, modifiers: false), "stop invalidates old timers")
        state.enable(keyAlreadyHeld: true)
        check(!state.keyDown(autorepeat: true, targetFrontmost: true, modifiers: false), "held at startup requires release")
        state.keyUp()
        check(state.keyDown(autorepeat: false, targetFrontmost: true, modifiers: false), "fresh press after startup release")
        check(!state.canSend(generation: state.generation, targetFrontmost: false, modifiers: false), "send rejects wrong target")
        check(!state.canSend(generation: state.generation, targetFrontmost: true, modifiers: true), "send rejects modifiers")

        check(RepeatSettings().valid, "default settings valid")
        check(!RepeatSettings(initialDelay: 230, minimumInterval: 121, maximumInterval: 120).valid, "inverted range rejected")
        check(!RepeatSettings(initialDelay: 0, minimumInterval: 85, maximumInterval: 120).valid, "invalid delay rejected")
        check(!RepeatSettings(initialDelay: 230, minimumInterval: 0, maximumInterval: 120).valid, "zero repeat interval rejected")
        let suite = "local.codex.maple-tests.\(UUID().uuidString)"
        let defaults = UserDefaults(suiteName: suite)!
        defer { defaults.removePersistentDomain(forName: suite) }
        let saved = RepeatSettings(initialDelay: 500, minimumInterval: 90, maximumInterval: 150)
        saved.save(to: defaults)
        let loaded = RepeatSettings.load(from: defaults)
        check(loaded.initialDelay == 500 && loaded.minimumInterval == 90 && loaded.maximumInterval == 150, "settings persist")
        defaults.set(-5, forKey: "minimumIntervalMS")
        check(RepeatSettings.load(from: defaults).minimumInterval == 85, "corrupt range restores defaults")
        defaults.set("broken", forKey: "initialDelayMS")
        defaults.set(85, forKey: "minimumIntervalMS")
        check(RepeatSettings.load(from: defaults).initialDelay == 230, "damaged stored value restores default")
        defaults.removePersistentDomain(forName: suite)
        defaults.set(450, forKey: "initialDelayMS")
        defaults.set(100, forKey: "minimumIntervalMS")
        defaults.set(160, forKey: "maximumIntervalMS")
        let migrated = RepeatSettings.load(from: defaults)
        check(migrated.attackTriggerKeyCode == 0 && migrated.initialDelay == 450
              && migrated.minimumInterval == 100 && migrated.maximumInterval == 160,
              "migration defaults to A and preserves existing timing settings")
        check(!RepeatSettings(attackTriggerKeyCode: 1).valid, "unrecognized trigger rejected")
        RepeatSettings(attackTriggerKeyCode: 0).save(to: defaults)
        check(RepeatSettings.load(from: defaults).attackTriggerKeyCode == 0, "legacy A trigger persists")
        RepeatSettings(attackTriggerKeyCode: 6).save(to: defaults)
        check(RepeatSettings.load(from: defaults).attackTriggerKeyCode == 6, "Z trigger persists")
        print("\(checks) checks passed; no event monitoring or posting used.")
    }
}
