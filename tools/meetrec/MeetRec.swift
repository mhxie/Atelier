// MeetRec: menu bar recorder for microphone + system audio, handed off to transcribe.py.
//
// One private aggregate device joins the default output device (clock), the
// default input device, and a global process tap, so both channels share one
// clock and need no post-hoc alignment. Devices are fixed when recording starts.
import AppKit
import AVFoundation
import AudioToolbox
import CoreAudio

struct RecError: Error, CustomStringConvertible {
    let description: String
}

func check(_ status: OSStatus, _ what: String) throws {
    if status != noErr { throw RecError(description: "\(what) failed (OSStatus \(status))") }
}

func address(_ sel: AudioObjectPropertySelector,
             _ scope: AudioObjectPropertyScope = kAudioObjectPropertyScopeGlobal) -> AudioObjectPropertyAddress {
    AudioObjectPropertyAddress(mSelector: sel, mScope: scope, mElement: kAudioObjectPropertyElementMain)
}

func defaultDevice(_ sel: AudioObjectPropertySelector) throws -> AudioDeviceID {
    var addr = address(sel)
    var id = AudioDeviceID(0)
    var size = UInt32(MemoryLayout<AudioDeviceID>.size)
    try check(AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject), &addr, 0, nil, &size, &id),
              "read default device")
    return id
}

func deviceUID(_ device: AudioDeviceID) throws -> String {
    var addr = address(kAudioDevicePropertyDeviceUID)
    var uid = "" as CFString
    var size = UInt32(MemoryLayout<CFString>.size)
    try check(withUnsafeMutablePointer(to: &uid) { AudioObjectGetPropertyData(device, &addr, 0, nil, &size, $0) },
              "read device UID")
    return uid as String
}

/// Number of input streams a device contributes to an aggregate, in HAL order.
func inputStreamCount(_ device: AudioDeviceID) -> Int {
    var addr = address(kAudioDevicePropertyStreams, kAudioObjectPropertyScopeInput)
    var size: UInt32 = 0
    guard AudioObjectGetPropertyDataSize(device, &addr, 0, nil, &size) == noErr else { return 0 }
    return Int(size) / MemoryLayout<AudioStreamID>.size
}

final class Recorder {
    private let capacity = 1 << 15
    private let micScratch: UnsafeMutablePointer<Float>
    private let sysScratch: UnsafeMutablePointer<Float>
    private var tapID = AudioObjectID(kAudioObjectUnknown)
    private var aggregateID = AudioObjectID(kAudioObjectUnknown)
    private var procID: AudioDeviceIOProcID?
    private var micFile: ExtAudioFileRef?
    private var sysFile: ExtAudioFileRef?
    private var micStreams = 0..<1
    private(set) var layoutFallback = false
    private(set) var captured = 0  // frames written since start; read by the silence watchdog

    init() {
        micScratch = .allocate(capacity: capacity)
        sysScratch = .allocate(capacity: capacity)
    }

    func start(into dir: URL) throws {
        do { try open(dir) } catch { stop(); throw error }
    }

    private func open(_ dir: URL) throws {
        let output = try defaultDevice(kAudioHardwarePropertyDefaultSystemOutputDevice)
        let input = try defaultDevice(kAudioHardwarePropertyDefaultInputDevice)
        let outputUID = try deviceUID(output)
        let inputUID = try deviceUID(input)

        let tap = CATapDescription(stereoGlobalTapButExcludeProcesses: [])
        tap.uuid = UUID()
        tap.muteBehavior = .unmuted  // .muted would silence the speakers while recording
        tap.isPrivate = true
        try check(AudioHardwareCreateProcessTap(tap, &tapID), "create system audio tap")

        // Aggregate input buffers arrive as each sub-device's input streams in list
        // order, then the tap. A headset exposes both directions under one UID.
        var subDevices: [[String: Any]] = [[kAudioSubDeviceUIDKey: outputUID]]
        var micStart = 0
        if inputUID != outputUID {
            micStart = inputStreamCount(output)
            subDevices.append([kAudioSubDeviceUIDKey: inputUID, kAudioSubDeviceDriftCompensationKey: true])
        }
        micStreams = micStart..<(micStart + max(inputStreamCount(input), 1))
        layoutFallback = false
        captured = 0

        let description: [String: Any] = [
            kAudioAggregateDeviceNameKey: "MeetRec",
            kAudioAggregateDeviceUIDKey: UUID().uuidString,
            kAudioAggregateDeviceMainSubDeviceKey: outputUID,
            kAudioAggregateDeviceIsPrivateKey: true,
            kAudioAggregateDeviceIsStackedKey: false,
            kAudioAggregateDeviceTapAutoStartKey: true,
            kAudioAggregateDeviceSubDeviceListKey: subDevices,
            kAudioAggregateDeviceTapListKey: [
                [kAudioSubTapDriftCompensationKey: true, kAudioSubTapUIDKey: tap.uuid.uuidString],
            ],
        ]
        try check(AudioHardwareCreateAggregateDevice(description as CFDictionary, &aggregateID),
                  "create aggregate device")

        var rateAddr = address(kAudioDevicePropertyNominalSampleRate)
        var rate = Float64(0)
        var size = UInt32(MemoryLayout<Float64>.size)
        try check(AudioObjectGetPropertyData(aggregateID, &rateAddr, 0, nil, &size, &rate), "read sample rate")

        micFile = try openWAV(dir.appendingPathComponent("mic.wav"), clientRate: rate)
        sysFile = try openWAV(dir.appendingPathComponent("system.wav"), clientRate: rate)
        try check(AudioDeviceCreateIOProcIDWithBlock(&procID, aggregateID, nil) { [unowned self] _, input, _, _, _ in
            self.capture(input)
        }, "create IO proc")
        try check(AudioDeviceStart(aggregateID, procID), "start recording")
    }

    /// 16 kHz mono int16 WAV; ExtAudioFile resamples from the native float client format.
    private func openWAV(_ url: URL, clientRate: Float64) throws -> ExtAudioFileRef {
        var fileFormat = AudioStreamBasicDescription(
            mSampleRate: 16_000, mFormatID: kAudioFormatLinearPCM,
            mFormatFlags: kLinearPCMFormatFlagIsSignedInteger | kLinearPCMFormatFlagIsPacked,
            mBytesPerPacket: 2, mFramesPerPacket: 1, mBytesPerFrame: 2, mChannelsPerFrame: 1,
            mBitsPerChannel: 16, mReserved: 0)
        var file: ExtAudioFileRef?
        try check(ExtAudioFileCreateWithURL(url as CFURL, kAudioFileWAVEType, &fileFormat, nil,
                                            AudioFileFlags.eraseFile.rawValue, &file), "create \(url.lastPathComponent)")
        guard let file else { throw RecError(description: "create \(url.lastPathComponent) returned no file") }
        var clientFormat = AudioStreamBasicDescription(
            mSampleRate: clientRate, mFormatID: kAudioFormatLinearPCM,
            mFormatFlags: kAudioFormatFlagIsFloat | kAudioFormatFlagIsPacked,
            mBytesPerPacket: 4, mFramesPerPacket: 1, mBytesPerFrame: 4, mChannelsPerFrame: 1,
            mBitsPerChannel: 32, mReserved: 0)
        try check(ExtAudioFileSetProperty(file, kExtAudioFileProperty_ClientDataFormat,
                                          UInt32(MemoryLayout<AudioStreamBasicDescription>.size), &clientFormat),
                  "set client format")
        try check(ExtAudioFileWriteAsync(file, 0, nil), "prime async writer")  // must happen off the IO thread
        return file
    }

    /// Realtime IO thread: downmix each side to mono and hand it to the async writers.
    private func capture(_ input: UnsafePointer<AudioBufferList>) {
        let buffers = UnsafeMutableAudioBufferListPointer(UnsafeMutablePointer(mutating: input))
        guard buffers.count >= 2 else { return }
        var mic = micStreams
        var tapIndex = mic.upperBound
        if tapIndex != buffers.count - 1 {
            mic = 0..<1
            tapIndex = buffers.count - 1
            layoutFallback = true
        }
        var frames = capacity
        for buffer in buffers {
            frames = min(frames, Int(buffer.mDataByteSize) / (4 * Int(max(buffer.mNumberChannels, 1))))
        }
        downmix(buffers, mic, into: micScratch, frames: frames)
        downmix(buffers, tapIndex..<(tapIndex + 1), into: sysScratch, frames: frames)
        write(micFile, micScratch, frames)
        write(sysFile, sysScratch, frames)
        captured += frames
    }

    private func downmix(_ buffers: UnsafeMutableAudioBufferListPointer, _ range: Range<Int>,
                         into out: UnsafeMutablePointer<Float>, frames: Int) {
        out.update(repeating: 0, count: frames)
        var channels = 0
        for index in range {
            let buffer = buffers[index]
            guard let data = buffer.mData?.assumingMemoryBound(to: Float.self) else { continue }
            let stride = Int(buffer.mNumberChannels)
            for frame in 0..<frames {
                for channel in 0..<stride { out[frame] += data[frame * stride + channel] }
            }
            channels += stride
        }
        if channels > 1 {
            let scale = 1 / Float(channels)
            for frame in 0..<frames { out[frame] *= scale }
        }
    }

    private func write(_ file: ExtAudioFileRef?, _ samples: UnsafeMutablePointer<Float>, _ frames: Int) {
        guard let file, frames > 0 else { return }
        var list = AudioBufferList(mNumberBuffers: 1, mBuffers: AudioBuffer(
            mNumberChannels: 1, mDataByteSize: UInt32(frames * 4), mData: UnsafeMutableRawPointer(samples)))
        ExtAudioFileWriteAsync(file, UInt32(frames), &list)
    }

    /// Idempotent; finalizes the WAV headers.
    func stop() {
        if let procID {
            AudioDeviceStop(aggregateID, procID)
            AudioDeviceDestroyIOProcID(aggregateID, procID)
        }
        procID = nil
        for file in [micFile, sysFile] { if let file { ExtAudioFileDispose(file) } }
        micFile = nil
        sysFile = nil
        if aggregateID != kAudioObjectUnknown { AudioHardwareDestroyAggregateDevice(aggregateID) }
        aggregateID = AudioObjectID(kAudioObjectUnknown)
        if tapID != kAudioObjectUnknown { AudioHardwareDestroyProcessTap(tapID) }
        tapID = AudioObjectID(kAudioObjectUnknown)
    }
}

final class Controller: NSObject, NSApplicationDelegate {
    private let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
    private let recorder = Recorder()
    private let defaults = UserDefaults.standard
    private var recording: (since: Date, dir: URL)?
    private var pending = 0  // transcriptions still running; recording stays available meanwhile
    private var lastSession: URL?
    private var timer: Timer?
    private var updateTimer: Timer?
    private var restarts = 0
    private var activity: NSObjectProtocol?
    /// `--record-for N`: record N seconds, transcribe, quit. Used for headless checks.
    private let autoSeconds: Double? = {
        let args = CommandLine.arguments
        guard let i = args.firstIndex(of: "--record-for"), i + 1 < args.count else { return nil }
        return Double(args[i + 1])
    }()

    private lazy var registry: [String: Any] = {
        guard let script = Bundle.main.infoDictionary?["MeetRecScript"] as? String,
              let data = try? Data(contentsOf: URL(fileURLWithPath: script).deletingLastPathComponent()
                  .appendingPathComponent("models.json")),
              let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return [:] }
        return json
    }()

    private var currentModel: String { defaults.string(forKey: "Model") ?? registry["default"] as? String ?? "" }

    private var outputRoot: URL {
        let path = defaults.string(forKey: "OutputDir") ?? "~/Recordings/meetings"
        return URL(fileURLWithPath: (path as NSString).expandingTildeInPath, isDirectory: true)
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        render()
        if autoSeconds != nil {
            toggle()
        } else if defaults.object(forKey: "CheckUpdates") as? Bool ?? true {
            checkUpdates()
            updateTimer = Timer.scheduledTimer(withTimeInterval: 86_400, repeats: true) { [weak self] _ in
                self?.checkUpdates()
            }
        }
    }

    func applicationWillTerminate(_ notification: Notification) {
        recorder.stop()  // keep the audio; transcribe.py can be rerun by hand
    }

    @objc func toggle() {
        if let recording { stopRecording(recording.dir) } else { withMicrophoneAccess { self.startRecording() } }
    }

    private func withMicrophoneAccess(_ then: @escaping () -> Void) {
        switch AVCaptureDevice.authorizationStatus(for: .audio) {
        case .authorized: then()
        case .notDetermined:
            AVCaptureDevice.requestAccess(for: .audio) { granted in
                DispatchQueue.main.async { granted ? then() : self.denied() }
            }
        default: denied()
        }
    }

    private func denied() {
        alert("Microphone access is off",
              "Allow MeetRec in System Settings > Privacy & Security > Microphone.")
    }

    private func startRecording() {
        let formatter = DateFormatter()
        formatter.dateFormat = "yyyy-MM-dd-HHmmss"
        let dir = outputRoot.appendingPathComponent(formatter.string(from: Date()), isDirectory: true)
        do {
            try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
            try recorder.start(into: dir)
        } catch {
            try? FileManager.default.removeItem(at: dir)
            alert("Could not start recording", "\(error)")
            return
        }
        recording = (Date(), dir)
        restarts = 0
        activity = ProcessInfo.processInfo.beginActivity(
            options: [.userInitiated, .idleSystemSleepDisabled], reason: "Recording a meeting")
        timer = Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { [weak self] _ in self?.tick() }
        render()
        if let seconds = autoSeconds {  // counted from the actual start, after any permission prompt
            DispatchQueue.main.asyncAfter(deadline: .now() + seconds) { [weak self] in self?.toggle() }
        }
    }

    private func stopRecording(_ dir: URL) {
        recorder.stop()
        timer?.invalidate()
        timer = nil
        if let activity { ProcessInfo.processInfo.endActivity(activity) }
        activity = nil
        if recorder.layoutFallback {
            try? "aggregate buffer layout differed from the device query; used first=mic, last=system\n"
                .write(to: dir.appendingPathComponent("recorder-warning.txt"), atomically: true, encoding: .utf8)
        }
        lastSession = dir
        recording = nil
        pending += 1
        render()
        transcribe(dir)
    }

    /// A permission prompt still open at start leaves the device silent: restart once, then warn.
    private func tick() {
        render()
        guard let recording, recorder.captured == 0 else { return }
        let elapsed = Date().timeIntervalSince(recording.since)
        if elapsed >= 3 && restarts == 0 {
            restarts = 1
            recorder.stop()
            try? recorder.start(into: recording.dir)
        } else if elapsed >= 8 && restarts == 1 {
            restarts = 2
            alert("No audio is arriving",
                  "Check Microphone and Screen & System Audio Recording for MeetRec in System Settings > "
                  + "Privacy & Security, then stop and start the recording again.")
        }
    }

    private func transcribe(_ dir: URL) {
        var args = [dir.path, "--out", transcriptURL(dir).path]
        for (key, flag) in [("Language", "--language"), ("Prompt", "--prompt"), ("Model", "--model")] {
            if let value = defaults.string(forKey: key), !value.isEmpty { args += [flag, value] }
        }
        runScript(args, log: dir.appendingPathComponent("transcribe.log")) { status, _ in
            self.finish(dir, ok: status == 0)
        }
    }

    /// Runs transcribe.py through uv. Finder launches get a minimal PATH, so build.sh embeds absolute paths.
    private func runScript(_ args: [String], log: URL? = nil, done: @escaping (Int32, Data) -> Void) {
        let info = Bundle.main.infoDictionary ?? [:]
        guard let uv = info["MeetRecUV"] as? String, let script = info["MeetRecScript"] as? String else {
            done(-1, Data())
            return
        }
        let process = Process()
        process.executableURL = URL(fileURLWithPath: uv)
        process.arguments = ["run", "--script", script] + args
        var env = ProcessInfo.processInfo.environment
        env["PATH"] = "\((uv as NSString).deletingLastPathComponent):/opt/homebrew/bin:/usr/bin:/bin"
        process.environment = env
        let pipe = Pipe()  // small JSON replies only; transcription output goes to its log file
        process.standardOutput = pipe
        process.standardError = FileHandle.nullDevice
        if let log, FileManager.default.createFile(atPath: log.path, contents: nil),
           let handle = try? FileHandle(forWritingTo: log) {
            process.standardOutput = handle
            process.standardError = handle
        }
        process.terminationHandler = { finished in
            let data = log == nil ? pipe.fileHandleForReading.readDataToEndOfFile() : Data()
            DispatchQueue.main.async { done(finished.terminationStatus, data) }
        }
        do { try process.run() } catch { done(-1, Data()) }
    }

    /// A new revision of a cached model waits for consent, since transcription loads it offline until
    /// then. A new model generation is only announced: it usually needs a newer runtime package.
    private func checkUpdates() {
        guard recording == nil else { return }  // never interrupt a meeting; the daily timer retries
        runScript(["--check-updates"]) { _, data in
            guard self.recording == nil,
                  let result = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return }
            var skipped = Set(self.defaults.stringArray(forKey: "SkippedRevisions") ?? [])
            for update in result["revisions"] as? [[String: String]] ?? [] {
                guard let repo = update["repo"], let remote = update["remote"],
                      !skipped.contains("\(repo)@\(remote)") else { continue }
                let panel = NSAlert()
                panel.messageText = "\(update["model"] ?? repo) has an update"
                panel.informativeText = "\(repo): \(update["local"]?.prefix(7) ?? "") -> \(remote.prefix(7)). "
                    + "Transcripts keep using the current version until the download finishes."
                for title in ["Update", "Skip This Version", "Later"] { panel.addButton(withTitle: title) }
                NSApp.activate(ignoringOtherApps: true)
                switch panel.runModal() {
                case .alertFirstButtonReturn:
                    self.runScript(["--prefetch", repo]) { status, _ in
                        self.notify(status == 0 ? "Model updated" : "Model update failed", repo)
                    }
                case .alertSecondButtonReturn: skipped.insert("\(repo)@\(remote)")
                default: break
                }
            }
            self.defaults.set(Array(skipped), forKey: "SkippedRevisions")
            var announced = Set(self.defaults.stringArray(forKey: "AnnouncedGenerations") ?? [])
            for generation in result["generations"] as? [[String: Any]] ?? [] {
                let name = "\(generation["family"] ?? "") \(generation["version"] ?? "")"
                if announced.insert(name).inserted {
                    self.notify("New model generation: \(name)", "Not added automatically; it may need a newer runtime")
                }
            }
            self.defaults.set(Array(announced), forKey: "AnnouncedGenerations")
        }
    }

    @objc func chooseModel(_ sender: NSMenuItem) {
        defaults.set(sender.representedObject as? String, forKey: "Model")
        render()
    }

    private func finish(_ dir: URL, ok: Bool) {
        pending -= 1
        render()
        notify(ok ? "Transcript ready" : "Transcription failed; see transcribe.log", dir.lastPathComponent)
        if autoSeconds != nil && pending == 0 && recording == nil { NSApp.terminate(nil) }
    }

    @objc func openTranscript() {
        guard let dir = lastSession else { return }
        let transcript = transcriptURL(dir)
        NSWorkspace.shared.open(FileManager.default.fileExists(atPath: transcript.path) ? transcript : dir)
    }

    /// The vault inbox embedded by build.sh (or the TranscriptDir default); else beside the audio.
    private func transcriptURL(_ dir: URL) -> URL {
        let configured = defaults.string(forKey: "TranscriptDir")
            ?? Bundle.main.infoDictionary?["MeetRecTranscriptDir"] as? String ?? ""
        if configured.isEmpty { return dir.appendingPathComponent("transcript.md") }
        return URL(fileURLWithPath: (configured as NSString).expandingTildeInPath, isDirectory: true)
            .appendingPathComponent("\(dir.lastPathComponent)-meeting.md")
    }

    @objc func openFolder() {
        try? FileManager.default.createDirectory(at: outputRoot, withIntermediateDirectories: true)
        NSWorkspace.shared.open(outputRoot)
    }

    private func render() {
        let menu = NSMenu()
        let button = item.button
        if let recording {
            let seconds = Int(Date().timeIntervalSince(recording.since))
            let clock = String(format: "%d:%02d:%02d", seconds / 3600, seconds / 60 % 60, seconds % 60)
            button?.image = symbol("record.circle.fill", .systemRed)
            button?.title = " " + clock
            menu.addItem(entry("Recording \(clock)", nil))
            menu.addItem(entry("Stop Recording", #selector(toggle), "r"))
        } else {
            button?.image = symbol(pending > 0 ? "waveform" : "mic", nil)
            button?.title = ""
            menu.addItem(entry("Start Recording", #selector(toggle), "r"))
        }
        if pending > 0 { menu.addItem(entry("Transcribing \(pending)...", nil)) }
        let models = NSMenu()
        for model in registry["models"] as? [[String: Any]] ?? [] {
            let key = model["key"] as? String ?? ""
            let choice = entry(model["label"] as? String ?? key, #selector(chooseModel(_:)))
            choice.representedObject = key
            choice.state = key == currentModel ? .on : .off
            models.addItem(choice)
        }
        let modelItem = NSMenuItem(title: "Model", action: nil, keyEquivalent: "")
        modelItem.submenu = models
        menu.addItem(.separator())
        menu.addItem(modelItem)
        menu.addItem(.separator())
        menu.addItem(entry("Open Last Transcript", lastSession == nil ? nil : #selector(openTranscript)))
        menu.addItem(entry("Open Recordings Folder", #selector(openFolder)))
        menu.addItem(.separator())
        menu.addItem(NSMenuItem(title: "Quit MeetRec", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q"))
        item.menu = menu
    }

    private func entry(_ title: String, _ action: Selector?, _ key: String = "") -> NSMenuItem {
        let menuItem = NSMenuItem(title: title, action: action, keyEquivalent: key)
        menuItem.target = self
        menuItem.isEnabled = action != nil
        return menuItem
    }

    private func symbol(_ name: String, _ color: NSColor?) -> NSImage? {
        let image = NSImage(systemSymbolName: name, accessibilityDescription: "MeetRec")
        guard let color else {
            image?.isTemplate = true
            return image
        }
        return image?.withSymbolConfiguration(.init(paletteColors: [color]))
    }

    private func alert(_ title: String, _ text: String) {
        NSApp.activate(ignoringOtherApps: true)
        let panel = NSAlert()
        panel.messageText = title
        panel.informativeText = text
        panel.runModal()
    }

    private func notify(_ text: String, _ subtitle: String) {
        let script = Process()
        script.executableURL = URL(fileURLWithPath: "/usr/bin/osascript")
        script.arguments = ["-e", "on run argv\ndisplay notification (item 1 of argv) with title \"MeetRec\" "
                            + "subtitle (item 2 of argv)\nend run", text, subtitle]
        try? script.run()
    }
}

let app = NSApplication.shared
let controller = Controller()
app.delegate = controller
app.setActivationPolicy(.accessory)
app.run()
