import SwiftUI
import AVFoundation

@MainActor
final class CompanionModel: ObservableObject {
    @Published var messages: [Message] = []
    @Published var favorites: [Keepsake] = []
    @Published var diaries: [Keepsake] = []
    @Published var pending: PendingMessage?
    @Published var sending = false
    @Published var error: String?
    @Published var connected = false
    @Published var playingID: String?
    private var audio: [String: Data] = [:]
    private var player: AVAudioPlayer?
    private let pendingKey = "pending_message_v1"
    var api: CompanionAPI {
        CompanionAPI(base: UserDefaults.standard.string(forKey: "server_url") ?? "", token: ConnectionKey.read())
    }
    init() {
        if let data = UserDefaults.standard.data(forKey: pendingKey) {
            pending = try? JSONDecoder().decode(PendingMessage.self, from: data)
        }
    }
    func refresh() async {
        guard !sending else { return }
        do {
            let result: History = try await api.request("v1/history")
            messages = result.messages
            connected = true
        } catch is CancellationError { }
        catch { connected = false }
    }
    func connect(base: String, token: String) async -> Bool {
        do {
            let connection = CompanionAPI(base: base.trimmingCharacters(in: .whitespacesAndNewlines),
                                          token: token.trimmingCharacters(in: .whitespacesAndNewlines))
            let result: History = try await connection.request("v1/history")
            try ConnectionKey.save(connection.token)
            UserDefaults.standard.set(connection.base, forKey: "server_url")
            messages = result.messages
            connected = true
            error = nil
            return true
        } catch { self.error = error.localizedDescription; return false }
    }
    func send(_ text: String) async {
        let content = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard pending == nil, !sending, !content.isEmpty else { return }
        pending = PendingMessage(id: UUID(), text: content, sentAt: Date().timeIntervalSince1970)
        UserDefaults.standard.set(try? JSONEncoder().encode(pending), forKey: pendingKey)
        await retry()
    }
    func retry() async {
        guard let request = pending, !sending else { return }
        sending = true
        error = nil
        do {
            let result = try await api.send(request)
            if let id = result.assistant_id,
               let encoded = result.replies.first(where: { $0.type == "record" })?.audio_base64,
               let data = Data(base64Encoded: encoded) {
                audio[id] = data
            }
            // A failed history refresh must not erase the already received reply.
            let answer = result.replies.compactMap { $0.text ?? $0.fallback_text }.joined(separator: "\n")
            messages.append(Message(id: request.id.uuidString + ":user", role: "user", text: request.text))
            if !answer.isEmpty {
                messages.append(Message(id: result.assistant_id ?? request.id.uuidString + ":assistant", role: "assistant", text: answer))
            }
            pending = nil
            UserDefaults.standard.removeObject(forKey: pendingKey)
            connected = true
        } catch { self.error = error.localizedDescription }
        sending = false
        await refresh()
    }
    func discardPending() {
        guard !sending else { return }
        pending = nil
        UserDefaults.standard.removeObject(forKey: pendingKey)
        error = nil
    }
    func loadKeepsakes() async {
        do {
            async let saved: Collection = api.request("v1/favorites")
            async let diary: Collection = api.request("v1/diaries")
            let result = try await (saved, diary)
            favorites = result.0.items
            diaries = result.1.items
        } catch { self.error = error.localizedDescription }
    }
    func hasAudio(_ id: String) -> Bool { audio[id] != nil }
    func play(_ id: String) {
        guard let data = audio[id] else { return }
        do {
            try AVAudioSession.sharedInstance().setCategory(.playback, mode: .spokenAudio)
            try AVAudioSession.sharedInstance().setActive(true)
            player = try AVAudioPlayer(data: data)
            player?.play()
            playingID = id
        } catch { self.error = "这条语音暂时无法播放。" }
    }
}
