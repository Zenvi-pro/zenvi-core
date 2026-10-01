// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "AppleSpeechAsr",
    platforms: [.macOS(.v14)],
    targets: [
        .executableTarget(
            name: "zenvi-apple-speech-asr",
            path: "Sources"
        ),
    ]
)
