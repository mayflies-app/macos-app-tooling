import AppKit
import SwiftUI

/// One native material owns previously unmounted content. Window behavior stays with the app.
@MainActor
enum MacAppGlass {
    struct Surface {
        let view: NSView
        let materialName: String
    }

    static func makeSurface(
        content: NSView,
        cornerRadius: CGFloat,
        forceStandard: Bool = false,
        interactive: Bool = false
    ) -> Surface {
        precondition(content.superview == nil, "MacAppGlass requires unparented content; do not wrap the same content twice.")
        precondition(!(content is NSVisualEffectView), "MacAppGlass requires content, not another material surface.")
        if #available(macOS 26.0, *) {
            precondition(!(content is NSGlassEffectView), "MacAppGlass requires content, not another material surface.")
        }
        precondition(cornerRadius.isFinite && cornerRadius >= 0, "MacAppGlass requires a finite, nonnegative corner radius.")

        let view: NSView
        let materialName: String
        if #available(macOS 26.0, *), !forceStandard,
           !NSWorkspace.shared.accessibilityDisplayShouldReduceTransparency {
            let glass = NSGlassEffectView()
            glass.style = .regular
            glass.cornerRadius = cornerRadius
            glass.contentView = content
            if #available(macOS 27.0, *) { glass.effectIsInteractive = interactive }
            view = glass
            materialName = "liquid-glass"
        } else {
            let effect = NSVisualEffectView()
            effect.material = .popover
            effect.blendingMode = .behindWindow
            effect.state = .active
            effect.wantsLayer = true
            effect.layer?.cornerRadius = cornerRadius
            effect.layer?.masksToBounds = true
            effect.addSubview(content)
            view = effect
            materialName = "standard"
        }
        content.translatesAutoresizingMaskIntoConstraints = false
        NSLayoutConstraint.activate([
            content.leadingAnchor.constraint(equalTo: view.leadingAnchor),
            content.trailingAnchor.constraint(equalTo: view.trailingAnchor),
            content.topAnchor.constraint(equalTo: view.topAnchor),
            content.bottomAnchor.constraint(equalTo: view.bottomAnchor)
        ])
        return Surface(view: view, materialName: materialName)
    }
}

/// Shared settings colors retain the established light/dark values and native sRGB conversion.
struct MacAppSettingsPalette {
    let dark: Bool
    var canvas: Color { color(dark ? 0x202226 : 0xF9FAFB) }
    var sidebar: Color { color(dark ? 0x191B1E : 0xEBEFF3) }
    var field: Color { color(dark ? 0x2B2E33 : 0xFFFFFF) }
    var selected: Color { color(dark ? 0x303A47 : 0xDCE5EF) }
    var segmentSelected: Color { color(dark ? 0x384A60 : 0xDEE8F3) }
    var hover: Color { color(dark ? 0x343C48 : 0xEDF2F7) }
    var pressed: Color { color(dark ? 0x3C4654 : 0xE4EBF3) }
    var border: Color { color(dark ? 0x41464E : 0xCCD4DD) }
    var text: Color { color(dark ? 0xECEEF1 : 0x26313D) }
    var secondary: Color { color(dark ? 0xA4ABB6 : 0x606F7D) }
    var accent: Color { color(dark ? 0x8DAAD0 : 0x5376A3) }
    var iconTile: Color { color(dark ? 0x303A47 : 0xF5F8FC) }
    var iconBorder: Color { color(dark ? 0x465261 : 0xCAD6E4) }
    var nativeCanvas: NSColor { native(dark ? 0x202226 : 0xF9FAFB) }
    var nativeField: NSColor { native(dark ? 0x2B2E33 : 0xFFFFFF) }
    var nativeHover: NSColor { native(dark ? 0x343C48 : 0xEDF2F7) }
    var nativeBorder: NSColor { native(dark ? 0x41464E : 0xCCD4DD) }
    var nativeAccent: NSColor { native(dark ? 0x8DAAD0 : 0x5376A3) }
    var nativeText: NSColor { native(dark ? 0xECEEF1 : 0x26313D) }

    private func native(_ hex: UInt32) -> NSColor {
        NSColor(srgbRed: CGFloat((hex >> 16) & 255) / 255, green: CGFloat((hex >> 8) & 255) / 255,
                blue: CGFloat(hex & 255) / 255, alpha: 1)
    }
    private func color(_ hex: UInt32) -> Color {
        Color(red: Double((hex >> 16) & 255) / 255, green: Double((hex >> 8) & 255) / 255,
              blue: Double(hex & 255) / 255)
    }
}

enum MacAppDesignTokens {
    static let controlRadius: CGFloat = 8
    static let symbolButtonExtent: CGFloat = 34
}
