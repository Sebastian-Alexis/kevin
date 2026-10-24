// Keep a Mac's built-in display at its own resolution while the ai-kvm capture display mirrors it.
// Without this, macOS can make the 1280x720 capture display the mirror master and drop the built-in
// panel to 720p. Saved permanently, so macOS reapplies it whenever the KVM reconnects.
// Usage: swift kvm-mirror.swift [WIDTHxHEIGHT]   built-in "looks like" size; default is Apple's default mode
import CoreGraphics
import Foundation

func fail(_ msg: String) -> Never {
    FileHandle.standardError.write((msg + "\n").data(using: .utf8)!)
    exit(1)
}

var ids = [CGDirectDisplayID](repeating: 0, count: 16)
var count: UInt32 = 0
guard CGGetOnlineDisplayList(16, &ids, &count) == .success else { fail("CGGetOnlineDisplayList failed") }
let online = ids.prefix(Int(count))
guard let builtin = online.first(where: { CGDisplayIsBuiltin($0) != 0 }) else { fail("no built-in display") }
guard let kvm = online.first(where: { CGDisplayIsBuiltin($0) == 0 }) else { fail("no external (KVM) display online") }

let opts = [kCGDisplayShowDuplicateLowResolutionModes: kCFBooleanTrue] as CFDictionary
let modes = (CGDisplayCopyAllDisplayModes(builtin, opts) as? [CGDisplayMode]) ?? []
let hidpi = modes.filter { $0.pixelWidth == 2 * $0.width && $0.isUsableForDesktopGUI() }
let mode: CGDisplayMode?
if CommandLine.arguments.count > 1 {
    let size = CommandLine.arguments[1].split(separator: "x").compactMap { Int($0) }
    guard size.count == 2 else { fail("size must be WIDTHxHEIGHT") }
    mode = hidpi.first { $0.width == size[0] && $0.height == size[1] }
} else {
    mode = hidpi.first { $0.ioFlags & 0x4 != 0 } // kDisplayModeDefaultFlag
}
guard let target = mode else {
    fail("no matching HiDPI mode; available: " + Set(hidpi.map { "\($0.width)x\($0.height)" }).sorted().joined(separator: " "))
}

var config: CGDisplayConfigRef?
guard CGBeginDisplayConfiguration(&config) == .success else { fail("CGBeginDisplayConfiguration failed") }
CGConfigureDisplayMirrorOfDisplay(config, builtin, kCGNullDirectDisplay) // built-in is never a mirror slave
CGConfigureDisplayMirrorOfDisplay(config, kvm, builtin)                  // KVM display mirrors the built-in
CGConfigureDisplayWithDisplayMode(config, builtin, target, nil)
let err = CGCompleteDisplayConfiguration(config, .permanently)
guard err == .success else {
    CGCancelDisplayConfiguration(config)
    fail("CGCompleteDisplayConfiguration failed: \(err.rawValue)")
}
print("built-in \(target.width)x\(target.height) HiDPI is the mirror master; KVM display \(kvm) mirrors it")
