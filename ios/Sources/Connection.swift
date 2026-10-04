import Foundation
import Security

enum ConnectionKey {
    private static let service = "com.ssrrrnn.companion.connection"
    static func read() -> String {
        let query: [String: Any] = [kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service, kSecAttrAccount as String: "token",
            kSecReturnData as String: true, kSecMatchLimit as String: kSecMatchLimitOne]
        var value: CFTypeRef?
        guard SecItemCopyMatching(query as CFDictionary, &value) == errSecSuccess,
              let data = value as? Data else { return "" }
        return String(data: data, encoding: .utf8) ?? ""
    }
    static func save(_ value: String) throws {
        let query: [String: Any] = [kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service, kSecAttrAccount as String: "token"]
        let data = Data(value.utf8)
        let update = SecItemUpdate(query as CFDictionary, [kSecValueData as String: data] as CFDictionary)
        if update == errSecSuccess { return }
        guard update == errSecItemNotFound else { throw ConnectionError.keychain }
        var insertion = query
        insertion[kSecValueData as String] = data
        insertion[kSecAttrAccessible as String] = kSecAttrAccessibleWhenUnlockedThisDeviceOnly
        guard SecItemAdd(insertion as CFDictionary, nil) == errSecSuccess else { throw ConnectionError.keychain }
    }
}

enum ConnectionError: LocalizedError {
    case setup, keychain, server(String)
    var errorDescription: String? {
        switch self {
        case .setup: return "先在设置里填写 HTTPS 地址和连接密钥。"
        case .keychain: return "密钥没有保存成功，请重试。"
        case .server(let message): return message
        }
    }
}

struct Message: Decodable, Identifiable {
    let id: String
    let role: String
    let text: String
}
struct Keepsake: Decodable, Identifiable { let id: String; let text: String }
struct History: Decodable { let messages: [Message] }
struct Collection: Decodable { let items: [Keepsake] }
struct ServerFailure: Decodable { let error: String }
struct Reply: Decodable {
    let type: String
    let text: String?
    let audio_base64: String?
    let fallback_text: String?
}
struct ChatResult: Decodable {
    let replies: [Reply]
    let assistant_id: String?
}
struct PendingMessage: Codable, Identifiable {
    let id: UUID
    let text: String
    let sentAt: Double
}

struct CompanionAPI {
    let base: String
    let token: String
    func request<T: Decodable>(_ path: String, body: Data? = nil) async throws -> T {
        guard let url = URL(string: base), url.scheme == "https", url.host != nil,
              url.user == nil, url.password == nil, url.query == nil, url.fragment == nil,
              token.count >= 32 else { throw ConnectionError.setup }
        var request = URLRequest(url: url.appendingPathComponent(path))
        request.timeoutInterval = 180
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        if let body {
            request.httpMethod = "POST"
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = body
        }
        let (data, response) = try await URLSession.shared.data(for: request)
        guard let response = response as? HTTPURLResponse else { throw ConnectionError.server("连接没有完成。") }
        guard (200..<300).contains(response.statusCode) else {
            let message = (try? JSONDecoder().decode(ServerFailure.self, from: data).error) ?? "连接没有完成，请稍后重试。"
            throw ConnectionError.server(message)
        }
        return try JSONDecoder().decode(T.self, from: data)
    }
    func send(_ pending: PendingMessage) async throws -> ChatResult {
        let body = try JSONSerialization.data(withJSONObject: [
            "request_id": pending.id.uuidString, "text": pending.text, "sent_at": pending.sentAt])
        return try await request("v1/chat", body: body)
    }
}
