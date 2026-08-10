import AVFoundation
import Foundation
import Speech

let helperVersion = 2

struct Options {
    var locale = "zh-CN"
    var maximumSeconds = 60.0
    var allowCloud = false
}

func writeResult(_ value: [String: Any], exitCode: Int32) -> Never {
    let data = try! JSONSerialization.data(withJSONObject: value, options: [])
    FileHandle.standardOutput.write(data)
    FileHandle.standardOutput.write(Data("\n".utf8))
    exit(exitCode)
}

func parseOptions() -> Options {
    var options = Options()
    var index = 1
    while index < CommandLine.arguments.count {
        let argument = CommandLine.arguments[index]
        if argument == "--locale", index + 1 < CommandLine.arguments.count {
            options.locale = CommandLine.arguments[index + 1]
            index += 2
        } else if argument == "--max-seconds", index + 1 < CommandLine.arguments.count {
            options.maximumSeconds = Double(CommandLine.arguments[index + 1]) ?? options.maximumSeconds
            index += 2
        } else if argument == "--allow-cloud" {
            options.allowCloud = true
            index += 1
        } else {
            writeResult(["status": "error", "reason": "invalid_arguments"], exitCode: 2)
        }
    }
    options.maximumSeconds = min(max(options.maximumSeconds, 1), 300)
    return options
}

func requestPermissions() -> String? {
    let speechSemaphore = DispatchSemaphore(value: 0)
    var speechStatus = SFSpeechRecognizer.authorizationStatus()
    if speechStatus == .notDetermined {
        SFSpeechRecognizer.requestAuthorization { status in
            speechStatus = status
            speechSemaphore.signal()
        }
        _ = speechSemaphore.wait(timeout: .now() + 30)
    }
    guard speechStatus == .authorized else {
        return "speech_recognition_permission_\(speechStatus.rawValue)"
    }

    let microphoneSemaphore = DispatchSemaphore(value: 0)
    var microphoneAllowed = AVCaptureDevice.authorizationStatus(for: .audio) == .authorized
    if AVCaptureDevice.authorizationStatus(for: .audio) == .notDetermined {
        AVCaptureDevice.requestAccess(for: .audio) { allowed in
            microphoneAllowed = allowed
            microphoneSemaphore.signal()
        }
        _ = microphoneSemaphore.wait(timeout: .now() + 30)
    }
    return microphoneAllowed ? nil : "microphone_permission_denied"
}

let options = parseOptions()
if let permissionError = requestPermissions() {
    writeResult(["status": "error", "reason": permissionError], exitCode: 3)
}

guard let recognizer = SFSpeechRecognizer(locale: Locale(identifier: options.locale)) else {
    writeResult(["status": "error", "reason": "unsupported_locale"], exitCode: 4)
}
guard recognizer.isAvailable else {
    writeResult(["status": "error", "reason": "recognizer_unavailable"], exitCode: 4)
}
if !options.allowCloud && !recognizer.supportsOnDeviceRecognition {
    writeResult(["status": "error", "reason": "on_device_recognition_unavailable"], exitCode: 4)
}

let engine = AVAudioEngine()
let request = SFSpeechAudioBufferRecognitionRequest()
request.shouldReportPartialResults = true
request.requiresOnDeviceRecognition = !options.allowCloud

let completion = DispatchSemaphore(value: 0)
let resultLock = NSLock()
var finalText = ""
var recognitionErrorDomain = ""
var recognitionErrorCode = 0
var audioBufferCount = 0
var audioPeak: Float = 0
let task = recognizer.recognitionTask(with: request) { result, error in
    resultLock.lock()
    if let result = result {
        finalText = result.bestTranscription.formattedString
    }
    if let error = error as NSError? {
        recognitionErrorDomain = error.domain
        recognitionErrorCode = error.code
    }
    let finished = result?.isFinal == true || error != nil
    resultLock.unlock()
    if finished {
        completion.signal()
    }
}

let input = engine.inputNode
let format = input.inputFormat(forBus: 0)
guard format.sampleRate > 0, format.channelCount > 0 else {
    task.cancel()
    writeResult(["status": "error", "reason": "microphone_unavailable"], exitCode: 5)
}
input.installTap(onBus: 0, bufferSize: 1024, format: format) { buffer, _ in
    resultLock.lock()
    audioBufferCount += 1
    if let channel = buffer.floatChannelData?[0] {
        for index in 0..<Int(buffer.frameLength) {
            audioPeak = max(audioPeak, abs(channel[index]))
        }
    }
    resultLock.unlock()
    request.append(buffer)
}

do {
    engine.prepare()
    try engine.start()
} catch {
    input.removeTap(onBus: 0)
    task.cancel()
    writeResult(["status": "error", "reason": "recording_failed", "error": String(describing: type(of: error))], exitCode: 5)
}

FileHandle.standardError.write(Data("Recording. Press Enter to stop.\n".utf8))
let stopSemaphore = DispatchSemaphore(value: 0)
DispatchQueue.global().async {
    _ = readLine()
    stopSemaphore.signal()
}
_ = stopSemaphore.wait(timeout: .now() + options.maximumSeconds)

engine.stop()
input.removeTap(onBus: 0)
request.endAudio()
_ = completion.wait(timeout: .now() + 15)

resultLock.lock()
let transcript = finalText.trimmingCharacters(in: .whitespacesAndNewlines)
let errorDomain = recognitionErrorDomain
let errorCode = recognitionErrorCode
let buffers = audioBufferCount
let peak = audioPeak
resultLock.unlock()

if transcript.isEmpty {
    task.cancel()
    let reason: String
    if buffers == 0 {
        reason = "no_audio_buffers"
    } else if peak < 0.001 {
        reason = "microphone_audio_silent"
    } else if !errorDomain.isEmpty {
        reason = "recognition_failed"
    } else {
        reason = "no_speech_recognized"
    }
    writeResult(
        [
            "status": "error",
            "reason": reason,
            "error_domain": errorDomain,
            "error_code": errorCode,
            "audio_buffers": buffers,
            "audio_peak": peak,
        ],
        exitCode: 6
    )
}
writeResult(
    [
        "status": "transcribed",
        "text": transcript,
        "locale": options.locale,
        "on_device": !options.allowCloud,
        "helper_version": helperVersion,
    ],
    exitCode: 0
)
