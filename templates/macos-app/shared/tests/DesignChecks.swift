import AppKit
import SwiftUI

@MainActor final class ProbeView: NSView {
    var focusRequests = 0
    var focusReleases = 0
    override var acceptsFirstResponder: Bool { true }
    override func becomeFirstResponder() -> Bool { focusRequests += 1; return true }
    override func resignFirstResponder() -> Bool { focusReleases += 1; return true }
}

@main struct DesignChecks {
    @MainActor static func main() {
        let app = NSApplication.shared
        let activationBefore = app.activationPolicy()
        let activeBefore = app.isActive
        let content = ProbeView(frame: .zero)
        content.setAccessibilityIdentifier("retained-content")
        let field = NSTextField(string: "Retained editable text")
        field.isEditable = true
        field.setAccessibilityLabel("Preserved editor")
        content.addSubview(field)
        let mode = CommandLine.arguments.dropFirst().first ?? "actual"
        if mode == "invalid-parent" {
            let owner = NSView(); owner.addSubview(content)
            _ = MacAppGlass.makeSurface(content: content, cornerRadius: 18)
            fatalError("Expected a contract failure before reparenting")
        }
        if mode == "invalid-radius" {
            _ = MacAppGlass.makeSurface(content: content, cornerRadius: -1)
            fatalError("Expected a contract failure for invalid radius")
        }
        if mode == "repeat-content" {
            let owned = MacAppGlass.makeSurface(content: content, cornerRadius: 18, forceStandard: true)
            withExtendedLifetime(owned) { _ = MacAppGlass.makeSurface(content: content, cornerRadius: 18) }
            fatalError("Expected a contract failure on reused content")
        }
        if mode == "repeat-surface" {
            let owned = MacAppGlass.makeSurface(content: content, cornerRadius: 18, forceStandard: true)
            _ = MacAppGlass.makeSurface(content: owned.view, cornerRadius: 18)
            fatalError("Expected a contract failure on nested material")
        }
        let forced = mode == "forced"
        let surface = MacAppGlass.makeSurface(content: content, cornerRadius: 18, forceStandard: forced, interactive: true)
        precondition(surface.view !== content)
        precondition(content.isDescendant(of: surface.view))
        precondition(content.subviews.count == 1 && content.subviews.first === field)
        precondition(field.superview === content && field.stringValue == "Retained editable text" && field.isEditable)
        precondition(field.accessibilityLabel() == "Preserved editor")
        precondition(content.accessibilityIdentifier() == "retained-content")
        precondition(content.acceptsFirstResponder && content.focusRequests == 0 && content.focusReleases == 0)
        precondition(app.activationPolicy() == activationBefore && app.isActive == activeBefore)
        precondition(surface.view.window == nil && content.window == nil)
        let edges: Set<NSLayoutConstraint.Attribute> = [.leading, .trailing, .top, .bottom]
        let pins = surface.view.constraints.filter { $0.isActive && ($0.firstItem as? NSView) === content && ($0.secondItem as? NSView) === surface.view }
        precondition(pins.count >= 4 && Set(pins.map(\.firstAttribute)) == edges)
        precondition(pins.allSatisfy { $0.firstAttribute == $0.secondAttribute && $0.constant == 0 && $0.relation == .equal })
        surface.view.frame = NSRect(x: 0, y: 0, width: 240, height: 120)
        surface.view.layoutSubtreeIfNeeded()
        precondition(content.frame.size == surface.view.bounds.size, "content must fill actual native surface layout")
        if surface.materialName == "standard" {
            guard let effect = surface.view as? NSVisualEffectView else { fatalError("Expected native visual effect fallback") }
            precondition(effect.material == .popover && effect.blendingMode == .behindWindow && effect.state == .active)
            precondition(effect.layer?.cornerRadius == 18 && effect.layer?.masksToBounds == true)
            precondition(effect.subviews.filter { $0 === content }.count == 1)
        } else {
            if #available(macOS 26.0, *) {
                guard let glass = surface.view as? NSGlassEffectView else { fatalError("Expected native glass") }
                precondition(glass.style == .regular && glass.cornerRadius == 18 && glass.contentView === content)
                if #available(macOS 27.0, *) { precondition(glass.effectIsInteractive) }
            } else { fatalError("Glass must not be constructed before macOS 26") }
        }
        if forced { precondition(surface.materialName == "standard") }
        if mode == "reduced" { precondition(surface.materialName == "standard", "Reduce Transparency must select native fallback") }
        if mode == "glass", #available(macOS 26.0, *) { precondition(surface.materialName == "liquid-glass") }
        checkPalette(dark: false)
        checkPalette(dark: true)
        precondition(MacAppDesignTokens.controlRadius == 8 && MacAppDesignTokens.symbolButtonExtent == 34)
        let output: [String: Any] = ["status": "passed", "mode": mode, "material": surface.materialName,
                                   "contentIdentityPreserved": true, "nativeLayoutFillsSurface": true, "nativeActiveEdgePinCount": pins.count,
                                   "accessibilityAndEditorPreserved": true, "focusCallbackCount": content.focusRequests,
                                   "activationUnchanged": true, "windowCreated": false,
                                   "paletteLightDarkExactSRGB": true, "reduceTransparencyRead": NSWorkspace.shared.accessibilityDisplayShouldReduceTransparency]
        print(String(data: try! JSONSerialization.data(withJSONObject: output, options: [.sortedKeys]), encoding: .utf8)!)
    }
    @MainActor static func checkPalette(dark: Bool) {
        let palette = MacAppSettingsPalette(dark: dark)
        let expected: [(NSColor, UInt32)] = [
            (palette.nativeCanvas, dark ? 0x202226 : 0xF9FAFB),
            (palette.nativeField, dark ? 0x2B2E33 : 0xFFFFFF),
            (palette.nativeHover, dark ? 0x343C48 : 0xEDF2F7),
            (palette.nativeBorder, dark ? 0x41464E : 0xCCD4DD),
            (palette.nativeAccent, dark ? 0x8DAAD0 : 0x5376A3),
            (palette.nativeText, dark ? 0xECEEF1 : 0x26313D)
        ]
        for (native, hex) in expected {
            guard let color = native.usingColorSpace(.sRGB) else { fatalError("Expected convertible sRGB color") }
            precondition(abs(color.redComponent - CGFloat((hex >> 16) & 255) / 255) < 0.000001)
            precondition(abs(color.greenComponent - CGFloat((hex >> 8) & 255) / 255) < 0.000001)
            precondition(abs(color.blueComponent - CGFloat(hex & 255) / 255) < 0.000001)
            precondition(color.alphaComponent == 1)
        }
        // Access every consumer-facing SwiftUI property as part of source/API compatibility.
        let colors = [palette.canvas, palette.sidebar, palette.field, palette.selected, palette.segmentSelected,
                      palette.hover, palette.pressed, palette.border, palette.text, palette.secondary,
                      palette.accent, palette.iconTile, palette.iconBorder]
        let hexes: [UInt32] = dark
            ? [0x202226,0x191B1E,0x2B2E33,0x303A47,0x384A60,0x343C48,0x3C4654,0x41464E,0xECEEF1,0xA4ABB6,0x8DAAD0,0x303A47,0x465261]
            : [0xF9FAFB,0xEBEFF3,0xFFFFFF,0xDCE5EF,0xDEE8F3,0xEDF2F7,0xE4EBF3,0xCCD4DD,0x26313D,0x606F7D,0x5376A3,0xF5F8FC,0xCAD6E4]
        for (value, hex) in zip(colors, hexes) {
            guard let color = NSColor(value).usingColorSpace(.sRGB) else { fatalError("Expected sRGB SwiftUI colors") }
            precondition(abs(color.redComponent - CGFloat((hex >> 16) & 255) / 255) < 0.000001)
            precondition(abs(color.greenComponent - CGFloat((hex >> 8) & 255) / 255) < 0.000001)
            precondition(abs(color.blueComponent - CGFloat(hex & 255) / 255) < 0.000001)
            precondition(color.alphaComponent == 1)
        }
    }
}
