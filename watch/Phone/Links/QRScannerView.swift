import SwiftUI
import UIKit
import VisionKit

/// "Scan QR code" (spec O5, mockup `iphone-06`): the camera reads a wristcall QR code and hands it to the
/// router. Without a camera (the simulator, or no permission) the link can be pasted. In server mode,
/// "Enter address instead" opens the manual form.
struct QRScannerView: View {
    let purpose: LinkRouter.ScanPurpose
    /// The router's token for this sheet: closing never closes a newer one.
    let token: Int
    @Environment(LinkRouter.self) private var router
    @State private var enteringAddress = false
    /// A read or pasted text that was not a wristcall link.
    @State private var hint: String?

    static let serverText = "Your wristcall server shows this code in its log the first time it starts. "
        + "To get a new one, run `wristcall link`"
    static let webText = "Open wristcall-app.trigram.com.br on your computer and scan the code it shows."
    static let noCamera = "The camera isn't available. Paste the link instead."
    static let notALink = "That isn't a wristcall link."
    static let nothingToPaste = "Copy the link first, then tap Paste link."

    /// Whether this device can scan now (`DataScannerViewController` needs a camera and permission).
    static var canScan: Bool { DataScannerViewController.isSupported && DataScannerViewController.isAvailable }

    var body: some View {
        NavigationStack {
            VStack(spacing: 0) {
                Spacer(minLength: 16)
                viewfinder
                Text(markdown: purpose == .server ? Self.serverText : Self.webText)
                    .font(.callout)
                    .foregroundStyle(.secondary)
                    .multilineTextAlignment(.center)
                    .padding(.top, 28)
                    .padding(.horizontal, 32)
                if let hint {
                    Text(hint)
                        .font(.callout)
                        .foregroundStyle(.orange)
                        .multilineTextAlignment(.center)
                        .padding(.top, 12)
                        .padding(.horizontal, 32)
                }
                Spacer(minLength: 24)
                VStack(spacing: 12) {
                    wideButton("Paste link", systemImage: "doc.on.clipboard") { paste() }
                    if purpose == .server {
                        wideButton("Enter address instead", systemImage: nil) { enteringAddress = true }
                    }
                }
                .padding(.horizontal, 20)
                .padding(.bottom, 12)
            }
            .frame(maxWidth: .infinity)
            .background(Color(white: 0.09).ignoresSafeArea())
            .navigationTitle("Scan QR code")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancel") { router.close(token) }
                }
            }
        }
        .environment(\.colorScheme, .dark)
        .sheet(isPresented: $enteringAddress) {
            AddServerView(onAdded: { router.close(token) })
        }
    }

    @ViewBuilder
    private var viewfinder: some View {
        let side: CGFloat = 250
        ZStack {
            if Self.canScan {
                QRCameraView { read($0) }
                    .clipShape(.rect(cornerRadius: 24))
            } else {
                VStack(spacing: 14) {
                    Image(systemName: "camera.fill")
                        .font(.system(size: 34))
                        .foregroundStyle(.secondary)
                    Text(Self.noCamera)
                        .font(.callout)
                        .foregroundStyle(.secondary)
                        .multilineTextAlignment(.center)
                        .padding(.horizontal, 24)
                }
            }
            ViewfinderCorners()
                .stroke(.white, style: StrokeStyle(lineWidth: 5, lineCap: .round))
                .accessibilityHidden(true)
        }
        .frame(width: side, height: side)
    }

    private func wideButton(_ title: String, systemImage: String?, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Group {
                if let systemImage { Label(title, systemImage: systemImage) } else { Text(title) }
            }
            .font(.headline)
            .frame(maxWidth: .infinity)
        }
        .buttonStyle(.bordered)
        .buttonBorderShape(.capsule)
        .controlSize(.large)
        .tint(.white)
    }

    private func read(_ text: String) {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let url = URL(string: trimmed), url.scheme != nil, router.open(url) else {
            hint = Self.notALink
            return
        }
        hint = nil
    }

    private func paste() {
        let board = UIPasteboard.general
        if let url = board.url {
            read(url.absoluteString)
        } else if let text = board.string, !text.isEmpty {
            read(text)
        } else {
            hint = Self.nothingToPaste
        }
    }
}

/// The four corners of the viewfinder.
private struct ViewfinderCorners: Shape {
    func path(in rect: CGRect) -> Path {
        let length = rect.width * 0.18
        let radius: CGFloat = 24
        var path = Path()
        // Top left.
        path.move(to: CGPoint(x: rect.minX, y: rect.minY + length))
        path.addLine(to: CGPoint(x: rect.minX, y: rect.minY + radius))
        path.addQuadCurve(to: CGPoint(x: rect.minX + radius, y: rect.minY), control: CGPoint(x: rect.minX, y: rect.minY))
        path.addLine(to: CGPoint(x: rect.minX + length, y: rect.minY))
        // Top right.
        path.move(to: CGPoint(x: rect.maxX - length, y: rect.minY))
        path.addLine(to: CGPoint(x: rect.maxX - radius, y: rect.minY))
        path.addQuadCurve(to: CGPoint(x: rect.maxX, y: rect.minY + radius), control: CGPoint(x: rect.maxX, y: rect.minY))
        path.addLine(to: CGPoint(x: rect.maxX, y: rect.minY + length))
        // Bottom right.
        path.move(to: CGPoint(x: rect.maxX, y: rect.maxY - length))
        path.addLine(to: CGPoint(x: rect.maxX, y: rect.maxY - radius))
        path.addQuadCurve(to: CGPoint(x: rect.maxX - radius, y: rect.maxY), control: CGPoint(x: rect.maxX, y: rect.maxY))
        path.addLine(to: CGPoint(x: rect.maxX - length, y: rect.maxY))
        // Bottom left.
        path.move(to: CGPoint(x: rect.minX + length, y: rect.maxY))
        path.addLine(to: CGPoint(x: rect.minX + radius, y: rect.maxY))
        path.addQuadCurve(to: CGPoint(x: rect.minX, y: rect.maxY - radius), control: CGPoint(x: rect.minX, y: rect.maxY))
        path.addLine(to: CGPoint(x: rect.minX, y: rect.maxY - length))
        return path
    }
}

/// VisionKit's live scanner for QR codes; each new code's text goes to `onCode` (the first one only, until
/// the code leaves the frame).
private struct QRCameraView: UIViewControllerRepresentable {
    let onCode: @MainActor (String) -> Void

    func makeUIViewController(context: Context) -> DataScannerViewController {
        let scanner = DataScannerViewController(
            recognizedDataTypes: [.barcode(symbologies: [.qr])],
            qualityLevel: .balanced,
            recognizesMultipleItems: false,
            isHighFrameRateTrackingEnabled: false,
            isHighlightingEnabled: true
        )
        scanner.delegate = context.coordinator
        return scanner
    }

    func updateUIViewController(_ scanner: DataScannerViewController, context: Context) {
        context.coordinator.onCode = onCode
        if !scanner.isScanning { try? scanner.startScanning() }
    }

    static func dismantleUIViewController(_ scanner: DataScannerViewController, coordinator: Coordinator) {
        scanner.stopScanning()
    }

    func makeCoordinator() -> Coordinator { Coordinator(onCode: onCode) }

    @MainActor
    final class Coordinator: NSObject, DataScannerViewControllerDelegate {
        var onCode: @MainActor (String) -> Void

        init(onCode: @escaping @MainActor (String) -> Void) {
            self.onCode = onCode
        }

        func dataScanner(
            _ dataScanner: DataScannerViewController, didAdd addedItems: [RecognizedItem], allItems: [RecognizedItem]
        ) {
            for item in addedItems {
                if case .barcode(let barcode) = item, let text = barcode.payloadStringValue {
                    onCode(text)
                    return
                }
            }
        }
    }
}
