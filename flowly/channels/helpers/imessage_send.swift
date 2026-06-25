// Flowly iMessage send helper.
//
// A code-signed .app bundle that sends one iMessage through Messages.app.
// It exists because TCC (Automation / Apple Events) permission is
// attributed to the *responsible process*: a bare `osascript` launched
// from the gateway's deep process chain (terminal → uv → python →
// osascript) gets an unreliable TCC identity and is refused with -10004
// / -1743. Launched through LaunchServices (`open`), THIS app is its own
// responsible process with a stable, user-nameable identity ("Flowly
// iMessage Helper") — so macOS surfaces the consent prompt once and the
// grant persists, independent of how the gateway itself was started.
//
// Invocation (by the Python channel, via `open -W -n <app> --args ...`):
//     <text> <target> [result-file]
// - target WITHOUT ';'  → bare handle → "buddy of account" form (the
//   form verified to deliver a DM on macOS 26/27).
// - target WITH ';'     → chat id form (groups / DM fallback).
// - result-file: when given, the outcome is written there ("OK" or
//   "ERR:<message>") and the process exits 0, so `open -W` stays clean
//   and the caller reads the file. Without it, success/failure is the
//   exit code + stderr.
//
// Message text and target are passed to AppleScript via environment
// (read with `system attribute`) so they can never be parsed as script.

import Foundation

let kTextEnv = "FLOWLY_IM_TEXT"
let kTargetEnv = "FLOWLY_IM_TARGET"

let args = CommandLine.arguments
guard args.count >= 3 else {
    FileHandle.standardError.write(
        Data("usage: imessage-send <text> <target> [result-file]\n".utf8))
    exit(2)
}
let text = args[1]
let target = args[2]
let resultPath: String? = args.count >= 4 ? args[3] : nil

func finish(_ errorMessage: String?) -> Never {
    if let path = resultPath {
        let content = errorMessage.map { "ERR:" + $0 } ?? "OK"
        try? content.write(toFile: path, atomically: true, encoding: .utf8)
        exit(0)  // keep `open -W` clean; the caller reads the file
    }
    if let message = errorMessage {
        FileHandle.standardError.write(Data((message + "\n").utf8))
        exit(1)
    }
    exit(0)
}

setenv(kTextEnv, text, 1)
setenv(kTargetEnv, target, 1)

let scriptSource: String
if target.contains(";") {
    scriptSource = """
    tell application "Messages"
        send (system attribute "\(kTextEnv)") to chat id (system attribute "\(kTargetEnv)")
    end tell
    """
} else {
    scriptSource = """
    tell application "Messages"
        set theAccount to 1st account whose service type = iMessage
        send (system attribute "\(kTextEnv)") to buddy (system attribute "\(kTargetEnv)") of theAccount
    end tell
    """
}

guard let script = NSAppleScript(source: scriptSource) else {
    finish("could not build AppleScript")
}
var errorInfo: NSDictionary?
script.executeAndReturnError(&errorInfo)
if let error = errorInfo {
    let message = error[NSAppleScript.errorMessage] as? String ?? "\(error)"
    let number = error[NSAppleScript.errorNumber] as? Int ?? 0
    finish("AppleScript error \(number): \(message)")
}
finish(nil)
