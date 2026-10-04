import SwiftUI

private let paper = Color(red: 0.98, green: 0.96, blue: 0.93)
private let wine = Color(red: 0.43, green: 0.23, blue: 0.30)

@main
struct CompanionApp: App {
    @StateObject private var model = CompanionModel()
    @Environment(\.scenePhase) private var scenePhase
    var body: some Scene {
        WindowGroup {
            CompanionTabs().environmentObject(model).tint(wine)
                .task(id: scenePhase) {
                    guard scenePhase == .active else { return }
                    while !Task.isCancelled {
                        await model.refresh()
                        do { try await Task.sleep(for: .seconds(15)) } catch { return }
                    }
                }
        }
    }
}

struct CompanionTabs: View {
    @EnvironmentObject private var model: CompanionModel
    @State private var selection = 0
    var body: some View {
        TabView(selection: $selection) {
            NavigationStack { HomeView(selection: $selection) }
                .tabItem { Label("小家", systemImage: "house") }.tag(0)
            NavigationStack { ChatView() }
                .tabItem { Label("聊天", systemImage: "bubble.left.and.bubble.right") }.tag(1)
            NavigationStack { KeepsakesView() }
                .tabItem { Label("珍藏", systemImage: "heart.text.square") }.tag(2)
            NavigationStack { SettingsView() }
                .tabItem { Label("设置", systemImage: "slider.horizontal.3") }.tag(3)
        }
    }
}

struct HomeView: View {
    @EnvironmentObject private var model: CompanionModel
    @AppStorage("companion_name") private var name = "他"
    @Binding var selection: Int
    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 26) {
                Text(Date.now, format: .dateTime.month().day().weekday()).font(.subheadline).foregroundStyle(.secondary)
                VStack(alignment: .leading, spacing: 14) {
                    Image(systemName: "lamp.table.fill").font(.system(size: 52)).foregroundStyle(wine)
                    Text("把日常，留在我们的小家。")
                        .font(.system(size: 31, weight: .medium, design: .serif))
                    Text("和\(name)聊聊天，听一听他的声音，看看一起留下的片段。")
                        .foregroundStyle(.secondary).lineSpacing(5)
                    Button { selection = model.connected ? 1 : 3 } label: {
                        Label(model.connected ? "去找他" : "连接我们的小家", systemImage: "arrow.right")
                    }.buttonStyle(.borderedProminent).controlSize(.large)
                }.padding(26).frame(maxWidth: .infinity, alignment: .leading)
                    .background(.white.opacity(0.75), in: RoundedRectangle(cornerRadius: 28))
                Text("我们的日常").font(.title3.weight(.medium))
                Button { selection = 2 } label: {
                    HStack { Image(systemName: "book.closed"); Text("日记与珍藏"); Spacer(); Image(systemName: "chevron.right") }
                        .padding(20).background(.white, in: RoundedRectangle(cornerRadius: 20))
                }.foregroundStyle(wine)
                if let last = model.messages.last(where: { $0.role == "assistant" }) {
                    Text("最近的一句话").font(.caption).foregroundStyle(.secondary)
                    Text(last.text).lineLimit(4).lineSpacing(5)
                }
                Text(model.connected ? "已经连接" : "尚未连接 · 在设置里连接现有伴侣")
                    .font(.caption).foregroundStyle(.secondary)
            }.padding(24)
        }.background(paper).navigationTitle("小家")
    }
}

struct ChatView: View {
    @EnvironmentObject private var model: CompanionModel
    @AppStorage("companion_name") private var name = "他"
    @State private var draft = ""
    var body: some View {
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(spacing: 15) {
                    if model.messages.isEmpty && model.pending == nil {
                        ContentUnavailableView("想说什么，就留在这里", systemImage: "bubble.left.and.bubble.right",
                            description: Text(model.connected ? "这里会延续你们已有的聊天和记忆。" : "先到设置连接你现有的伴侣。"))
                    }
                    ForEach(model.messages) { message in bubble(message) }
                    if let pending = model.pending {
                        bubble(Message(id: pending.id.uuidString, role: "user", text: pending.text))
                        HStack {
                            if model.sending { ProgressView(); Text("他正在回复") }
                            else {
                                Button("重试发送") { Task { await model.retry() } }
                                Button("取消待发送") { model.discardPending() }
                            }
                        }.font(.caption).foregroundStyle(.secondary)
                    }
                    if let error = model.error { Text(error).font(.caption).foregroundStyle(.red).padding(.horizontal) }
                    Color.clear.frame(height: 1).id("bottom")
                }.padding(18)
            }.background(paper)
                .onChange(of: model.messages.count) { _, _ in withAnimation { proxy.scrollTo("bottom", anchor: .bottom) } }
                .onChange(of: model.pending?.id) { _, _ in withAnimation { proxy.scrollTo("bottom", anchor: .bottom) } }
                .safeAreaInset(edge: .bottom) {
                    HStack(alignment: .bottom, spacing: 12) {
                        TextField("想和他说的话", text: $draft, axis: .vertical).lineLimit(1...5)
                            .padding(12).background(.white, in: RoundedRectangle(cornerRadius: 18))
                        Button {
                            let text = draft
                            draft = ""
                            Task { await model.send(text) }
                        } label: { Image(systemName: "arrow.up.circle.fill").font(.system(size: 35)) }
                            .disabled(draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || draft.count > 4000 || model.pending != nil || model.sending || !model.connected)
                    }.padding(12).background(paper)
                }
        }.navigationTitle(name).navigationBarTitleDisplayMode(.inline)
            .toolbar { Button { Task { await model.refresh() } } label: { Image(systemName: "arrow.clockwise") } }
    }
    private func bubble(_ message: Message) -> some View {
        HStack {
            if message.role == "user" { Spacer(minLength: 45) }
            VStack(alignment: .leading, spacing: 10) {
                Text(message.text).textSelection(.enabled).lineSpacing(4)
                if model.hasAudio(message.id) {
                    Button { model.play(message.id) } label: { Label("听他的声音", systemImage: "waveform") }
                }
            }.padding(15).foregroundStyle(message.role == "user" ? Color.white : Color.primary)
                .background(message.role == "user" ? wine : Color.white, in: RoundedRectangle(cornerRadius: 20))
            if message.role != "user" { Spacer(minLength: 45) }
        }
    }
}

struct KeepsakesView: View {
    @EnvironmentObject private var model: CompanionModel
    @State private var diaries = true
    var body: some View {
        VStack {
            Picker("珍藏", selection: $diaries) { Text("日记").tag(true); Text("收藏").tag(false) }
                .pickerStyle(.segmented).padding()
            ScrollView {
                LazyVStack(spacing: 16) {
                    let items = diaries ? model.diaries : model.favorites
                    if items.isEmpty { ContentUnavailableView("还没有留下片段", systemImage: "book",
                        description: Text("连接后，这里会显示现有的日记和收藏。")) }
                    ForEach(items) { item in
                        Text(item.text).lineSpacing(6).textSelection(.enabled)
                            .frame(maxWidth: .infinity, alignment: .leading).padding(22)
                            .background(.white, in: RoundedRectangle(cornerRadius: 20))
                    }
                }.padding(.horizontal, 20)
            }.refreshable { await model.loadKeepsakes() }
            if let error = model.error { Text(error).font(.caption).foregroundStyle(.red).padding() }
        }.background(paper).navigationTitle("留给我们的")
            .task { if model.connected { await model.loadKeepsakes() } }
    }
}

struct SettingsView: View {
    @EnvironmentObject private var model: CompanionModel
    @AppStorage("companion_name") private var name = "他"
    @State private var base = UserDefaults.standard.string(forKey: "server_url") ?? ""
    @State private var token = ConnectionKey.read()
    @State private var connecting = false
    @State private var saved = false
    var body: some View {
        Form {
            Section("我们的小家") { TextField("他的名字", text: $name) }
            Section {
                TextField("HTTPS 服务地址", text: $base).keyboardType(.URL).textInputAutocapitalization(.never).autocorrectionDisabled()
                SecureField("连接密钥", text: $token).textInputAutocapitalization(.never).autocorrectionDisabled()
                Button(connecting ? "正在连接" : "保存并连接") {
                    connecting = true
                    Task { saved = await model.connect(base: base, token: token); connecting = false }
                }.disabled(connecting || model.sending || model.pending != nil)
                if saved { Label("已经连接", systemImage: "checkmark.circle").foregroundStyle(.green) }
                if let error = model.error { Text(error).foregroundStyle(.red) }
            } header: { Text("连接现有伴侣") }
              footer: { Text("填写自己的伴侣服务地址与连接密钥。Claude、Fish Audio 密钥由服务端保存。") }
            Section("当前版本") {
                Text("小家 · 0.1")
                Text("聊天、语音播放、日记和收藏。主动消息继续通过 QQ 或 Telegram 到达，打开小家可以查看。").font(.footnote)
            }
        }.navigationTitle("设置")
    }
}
