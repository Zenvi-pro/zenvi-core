import AVFoundation
import Foundation
import Speech

/// CLI: zenvi-apple-speech-asr <audioPath> [--locale BCP47]
/// Prints JSON: {engine, language, words:[{text,startSec,endSec}]}
/// Exit 0 ok, 2 unavailable (OS too old) → Zenvi falls back to Whisper, 1 error.

@main
struct ZenviAppleSpeechAsr {
    static func main() async {
        let args = Array(CommandLine.arguments.dropFirst())
        guard let path = args.first else {
            fputs("usage: zenvi-apple-speech-asr <audioPath> [--locale bcp47]\n", stderr)
            exit(1)
        }
        var localeId: String? = nil
        if let i = args.firstIndex(of: "--locale"), i + 1 < args.count {
            localeId = args[i + 1]
        }
        let url = URL(fileURLWithPath: path)
        guard FileManager.default.fileExists(atPath: path) else {
            fputs("file not found: \(path)\n", stderr)
            exit(1)
        }

        if #available(macOS 26.0, *) {
            do {
                let result = try await transcribe(fileURL: url, preferredLocaleId: localeId)
                let data = try JSONSerialization.data(withJSONObject: result, options: [.sortedKeys])
                if let s = String(data: data, encoding: .utf8) {
                    print(s)
                }
                exit(0)
            } catch {
                fputs("apple speech failed: \(error.localizedDescription)\n", stderr)
                exit(1)
            }
        } else {
            fputs("SpeechAnalyzer requires macOS 26+; use whisper fallback\n", stderr)
            exit(2)
        }
    }

    @available(macOS 26.0, *)
    static func transcribe(fileURL: URL, preferredLocaleId: String?) async throws -> [String: Any] {
        let supported = await SpeechTranscriber.supportedLocales
        let locale: Locale
        if let preferredLocaleId,
           let match = supported.first(where: {
               $0.identifier(.bcp47).lowercased() == preferredLocaleId.lowercased()
                   || $0.identifier.lowercased().hasPrefix(preferredLocaleId.lowercased())
           }) {
            locale = match
        } else if let auto = supported.first(where: {
            $0.identifier(.bcp47).hasPrefix(Locale.current.language.languageCode?.identifier ?? "en")
        }) ?? supported.first {
            locale = auto
        } else {
            throw NSError(
                domain: "zenvi-apple-speech-asr",
                code: 2,
                userInfo: [NSLocalizedDescriptionKey: "no supported SpeechTranscriber locale"]
            )
        }

        let transcriber = SpeechTranscriber(
            locale: locale,
            transcriptionOptions: [],
            reportingOptions: [],
            attributeOptions: [.audioTimeRange]
        )

        if let install = try await AssetInventory.assetInstallationRequest(supporting: [transcriber]) {
            try await install.downloadAndInstall()
        }

        let audioFile = try AVAudioFile(forReading: fileURL)
        let analyzer = SpeechAnalyzer(modules: [transcriber])

        let resultsTask = Task { () throws -> [SpeechTranscriber.Result] in
            var acc: [SpeechTranscriber.Result] = []
            for try await result in transcriber.results {
                acc.append(result)
            }
            return acc
        }

        if let lastSampleTime = try await analyzer.analyzeSequence(from: audioFile) {
            try await analyzer.finalizeAndFinish(through: lastSampleTime)
        } else {
            await analyzer.cancelAndFinishNow()
        }

        let collected = try await resultsTask.value
        var words: [[String: Any]] = []
        for result in collected {
            let attributed = result.text
            for run in attributed.runs {
                let runText = String(attributed[run.range].characters)
                    .trimmingCharacters(in: .whitespacesAndNewlines)
                if runText.isEmpty { continue }
                guard let range = run.audioTimeRange else { continue }
                let start = range.start.seconds
                let end = (range.start + range.duration).seconds
                guard end > start, start.isFinite, end.isFinite else { continue }
                words.append([
                    "text": runText,
                    "startSec": start,
                    "endSec": end,
                ])
            }
        }

        return [
            "engine": "apple-speech-analyzer",
            "language": locale.identifier(.bcp47),
            "words": words,
        ]
    }
}
