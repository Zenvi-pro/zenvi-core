# Apple SpeechAnalyzer helper (macOS 26+)
#
# Build (on a Mac with Xcode / Swift 6):
#   cd native/apple-speech-asr && swift build -c release
# Binary lands at:
#   .build/release/zenvi-apple-speech-asr
#
# Zenvi looks for it via PATH, ZENVI_APPLE_SPEECH_ASR, or this .build path.
# If missing or exit 2 (old macOS), transcription falls back to faster-whisper.
