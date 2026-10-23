import AppKit
// Test-only lexical substitute for the single workspace preference read.
// The native material, layout, content ownership and accessibility checks remain real AppKit.
@MainActor enum NSWorkspace {
    static let shared = WorkspacePreference()
}
@MainActor final class WorkspacePreference {
    var accessibilityDisplayShouldReduceTransparency: Bool { CommandLine.arguments.contains("reduced") }
}
