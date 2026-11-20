// Hell Grind — Mac uygulaması.
//
// İnce bir kabuk: hattı her zamanki gibi Docker Desktop'ta (docker compose) çalıştırır, paneli kendi penceresinde açar.
// Hiçbir ücretli servisi çağırmaz, .env'e dokunmaz (yalnızca yoksa .env.example'dan oluşturur); mod seçimi her zaman
// panelin Ayarlar sayfasında ya da .env'de kalır.
import AppKit
import WebKit

let localBase = "http://localhost:8000"
/// Uzak sunucu seçildiyse onun adresi (ör. Tailscale ile http://enes-mac:8000), değilse bu Mac.
var apiBase: String { Prefs.serverURL ?? localBase }
var panelURL: URL { URL(string: "\(apiBase)/panel/")! }
var healthURL: URL { URL(string: "\(apiBase)/healthz")! }
var isRemote: Bool { Prefs.serverURL != nil }
let composeProject = "hellgrind"   // `make up` ile aynı ad: aynı birimler (veritabanı, MinIO), aynı veri

// MARK: - Ayarlar

enum Prefs {
    static let d = UserDefaults.standard
    /// Kullanıcının seçtiği mevcut repo klasörü; boşsa uygulamanın kendi kopyası kullanılır.
    static var stackPath: String? {
        get { d.string(forKey: "stackPath") }
        set { d.set(newValue, forKey: "stackPath") }
    }
    static var chosen: Bool {
        get { d.bool(forKey: "stackChosen") }
        set { d.set(newValue, forKey: "stackChosen") }
    }
    /// Uzak sunucu kipi: hat başka bir bilgisayarda çalışır, bu uygulama yalnızca paneli açar (Docker gerekmez).
    static var serverURL: String? {
        get { d.string(forKey: "serverURL").flatMap { $0.isEmpty ? nil : $0 } }
        set { d.set(newValue, forKey: "serverURL") }
    }
    static var stopOnQuit: Bool {
        get { d.bool(forKey: "stopOnQuit") }
        set { d.set(newValue, forKey: "stopOnQuit") }
    }
}

// MARK: - Hat klasörü

enum Stack {
    static var supportDir: URL {
        let base = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
        return base.appendingPathComponent("Hellgrind", isDirectory: true)
    }
    static var bundledDir: URL? { Bundle.main.resourceURL?.appendingPathComponent("stack", isDirectory: true) }
    static var ownCopyDir: URL { supportDir.appendingPathComponent("stack", isDirectory: true) }

    /// Çalışılacak klasör: seçilmiş repo ya da uygulamanın kendi kopyası.
    static var dir: URL {
        if let p = Prefs.stackPath, !p.isEmpty { return URL(fileURLWithPath: p, isDirectory: true) }
        return ownCopyDir
    }
    static var usesOwnCopy: Bool { (Prefs.stackPath ?? "").isEmpty }

    static func version(at dir: URL) -> String? {
        try? String(contentsOf: dir.appendingPathComponent(".bundle-version"), encoding: .utf8)
            .trimmingCharacters(in: .whitespacesAndNewlines)
    }

    /// Uygulamanın içindeki kaynak kopyasını Application Support'a kurar ya da günceller. .env, panelin yazdığı
    /// ComfyUI iş akışları ve celery beat dosyası korunur.
    static func installOwnCopyIfNeeded(log: (String) -> Void) throws {
        guard usesOwnCopy, let src = bundledDir, FileManager.default.fileExists(atPath: src.path) else { return }
        let want = version(at: src)
        if FileManager.default.fileExists(atPath: ownCopyDir.path), version(at: ownCopyDir) == want { return }
        try FileManager.default.createDirectory(at: ownCopyDir, withIntermediateDirectories: true)
        log("Hat dosyaları kuruluyor (\(want ?? "?")) → \(ownCopyDir.path)")
        let s = src.path + "/", t = ownCopyDir.path + "/"
        // 1) Kod: .env ve panelin yazdığı dosyalar hariç birebir eşitlenir.
        try runSync("/usr/bin/rsync", ["-a", "--delete",
                                       "--exclude", "/.env", "--exclude", "/.env.bak*",
                                       "--exclude", "/comfy-workflows/", "--exclude", "/backend/celerybeat-schedule*",
                                       s, t])
        // 2) ComfyUI iş akışları: yalnızca eksik olanlar eklenir (panelden yüklenenler ezilmez).
        try runSync("/usr/bin/rsync", ["-a", "--ignore-existing", s + "comfy-workflows/", t + "comfy-workflows/"])
    }

    /// .env yoksa .env.example'dan oluşturur (ücretsiz mod: DRY_RUN=1).
    static func ensureEnv(log: (String) -> Void) {
        let env = dir.appendingPathComponent(".env"), example = dir.appendingPathComponent(".env.example")
        guard !FileManager.default.fileExists(atPath: env.path),
              FileManager.default.fileExists(atPath: example.path) else { return }
        do {
            try FileManager.default.copyItem(at: example, to: env)
            try? FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: env.path)
            log(".env bulunamadı; .env.example'dan oluşturuldu (ücretsiz mod).")
        } catch { log(".env oluşturulamadı: \(error.localizedDescription)") }
    }

    static func looksLikeRepo(_ url: URL) -> Bool {
        FileManager.default.fileExists(atPath: url.appendingPathComponent("docker-compose.yml").path)
            && FileManager.default.fileExists(atPath: url.appendingPathComponent("backend").path)
    }
}

@discardableResult
func runSync(_ exe: String, _ args: [String]) throws -> Int32 {
    let p = Process()
    p.executableURL = URL(fileURLWithPath: exe)
    p.arguments = args
    try p.run()
    p.waitUntilExit()
    if p.terminationStatus != 0 {
        throw NSError(domain: "hg", code: Int(p.terminationStatus),
                      userInfo: [NSLocalizedDescriptionKey: "\(exe) \(p.terminationStatus) koduyla bitti"])
    }
    return p.terminationStatus
}

// MARK: - Docker

enum Docker {
    static let binDirs = ["/usr/local/bin", "/opt/homebrew/bin", "/Applications/Docker.app/Contents/Resources/bin",
                          NSHomeDirectory() + "/.docker/bin"]

    static var env: [String: String] {
        var e = ProcessInfo.processInfo.environment
        let path = (binDirs + ["/usr/bin", "/bin", "/usr/sbin", "/sbin"]).joined(separator: ":")
        e["PATH"] = path + (e["PATH"].map { ":" + $0 } ?? "")
        return e
    }

    static var binary: String? {
        binDirs.map { $0 + "/docker" }.first { FileManager.default.isExecutableFile(atPath: $0) }
    }

    static var desktopInstalled: Bool {
        FileManager.default.fileExists(atPath: "/Applications/Docker.app")
            || FileManager.default.fileExists(atPath: NSHomeDirectory() + "/Applications/Docker.app")
    }

    /// `docker info` başarılıysa motor ayaktadır.
    static func engineUp() -> Bool {
        guard let bin = binary else { return false }
        let p = Process()
        p.executableURL = URL(fileURLWithPath: bin)
        p.arguments = ["info", "--format", "{{.ServerVersion}}"]
        p.environment = env
        p.standardOutput = FileHandle.nullDevice
        p.standardError = FileHandle.nullDevice
        do { try p.run() } catch { return false }
        p.waitUntilExit()
        return p.terminationStatus == 0
    }

    static func composeArgs(_ rest: [String]) -> [String] {
        ["compose", "-p", composeProject] + rest
    }
}

/// Uzun süren bir komutu çalıştırır, çıktısını satır satır iletir.
final class Job {
    let process = Process()
    private var buffer = Data()

    init(_ exe: String, _ args: [String], cwd: URL, onLine: @escaping (String) -> Void,
         onExit: @escaping (Int32) -> Void) {
        process.executableURL = URL(fileURLWithPath: exe)
        process.arguments = args
        process.currentDirectoryURL = cwd
        process.environment = Docker.env
        let pipe = Pipe()
        process.standardOutput = pipe
        process.standardError = pipe
        pipe.fileHandleForReading.readabilityHandler = { [weak self] h in
            let chunk = h.availableData
            guard let self = self else { return }
            if chunk.isEmpty { h.readabilityHandler = nil; return }
            self.buffer.append(chunk)
            while let nl = self.buffer.firstIndex(where: { $0 == 0x0A || $0 == 0x0D }) {
                let line = String(decoding: self.buffer[self.buffer.startIndex..<nl], as: UTF8.self)
                self.buffer.removeSubrange(self.buffer.startIndex...nl)
                if !line.trimmingCharacters(in: .whitespaces).isEmpty { DispatchQueue.main.async { onLine(line) } }
            }
        }
        process.terminationHandler = { p in
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.2) { onExit(p.terminationStatus) }
        }
    }

    func start() throws { try process.run() }
}

// MARK: - Bekleme ekranı

let loadingHTML = """
<!doctype html><html lang="tr"><head><meta charset="utf-8"><style>
:root{color-scheme:dark}
body{margin:0;background:#19191c;color:#d9d9dd;font:14px/1.5 -apple-system,Inter,sans-serif;display:flex;flex-direction:column;height:100vh}
header{padding:28px 32px 12px}
h1{margin:0 0 4px;font-size:22px;font-weight:600}
h1 b{color:#e8783a}
#status{font-size:15px}
#detail{color:#9a9aa3;margin-top:4px;white-space:pre-wrap}
.err #status{color:#ff5c55}
.spin{display:inline-block;width:10px;height:10px;border-radius:50%;background:#a88bff;margin-right:8px;animation:p 1.2s ease-in-out infinite}
.err .spin,.idle .spin{display:none}
@keyframes p{50%{opacity:.25}}
@media (prefers-reduced-motion:reduce){.spin{animation:none}}
#actions{margin-top:12px;display:flex;gap:8px;flex-wrap:wrap}
button{background:#2b2b30;color:#d9d9dd;border:1px solid #3a3a41;border-radius:4px;padding:6px 12px;font:inherit;cursor:pointer}
button.primary{background:#e8783a;border-color:#e8783a;color:#19191c}
button:focus-visible{outline:2px solid #e8783a;outline-offset:2px}
pre{flex:1;margin:12px 32px 24px;padding:12px;background:#232327;border:1px solid #3a3a41;border-radius:4px;overflow:auto;
 font:12px/1.45 "JetBrains Mono",Menlo,monospace;color:#b8b8c0;white-space:pre-wrap;word-break:break-all}
</style></head><body><header>
<h1><b>Hell</b> Grind</h1>
<div id="status"><span class="spin"></span><span id="st">Başlatılıyor…</span></div>
<div id="detail"></div><div id="actions"></div></header>
<pre id="log"></pre>
<script>
function hg(a){window.webkit.messageHandlers.hg.postMessage(a)}
function setStatus(s,d,mode,btns){
 document.body.className=mode||'';document.getElementById('st').textContent=s;
 document.getElementById('detail').textContent=d||'';
 var box=document.getElementById('actions');box.innerHTML='';
 (btns||[]).forEach(function(b,i){var e=document.createElement('button');e.textContent=b[1];if(i==0)e.className='primary';
  e.onclick=function(){hg(b[0])};box.appendChild(e)});
}
function addLog(lines){var p=document.getElementById('log');var stick=p.scrollTop+p.clientHeight>=p.scrollHeight-20;
 p.textContent+=lines;if(p.textContent.length>400000)p.textContent=p.textContent.slice(-300000);if(stick)p.scrollTop=p.scrollHeight}
</script></body></html>
"""

// MARK: - Uygulama

final class AppDelegate: NSObject, NSApplicationDelegate, WKNavigationDelegate, WKUIDelegate,
                         WKScriptMessageHandler, WKDownloadDelegate {
    var window: NSWindow!
    var web: WKWebView!
    var job: Job?
    var pendingLog = ""
    var flushTimer: Timer?
    var onPanel = false
    var pageReady = false          // bekleme sayfası yüklendi mi (JS köprüsü hazır mı)
    var lastStatus: String?        // sayfa hazır olmadan gelen son durum, yüklenince yeniden basılır
    var starting = false
    var logWindow: NSWindow?
    var logView: NSTextView?
    var stopMenuItem: NSMenuItem?

    func applicationDidFinishLaunching(_ n: Notification) {
        buildMenu()
        let cfg = WKWebViewConfiguration()
        cfg.userContentController.add(self, name: "hg")
        cfg.websiteDataStore = .default()
        if #available(macOS 12.3, *) { cfg.preferences.isElementFullscreenEnabled = true }
        cfg.mediaTypesRequiringUserActionForPlayback = []
        web = WKWebView(frame: .zero, configuration: cfg)
        web.navigationDelegate = self
        web.uiDelegate = self
        web.allowsBackForwardNavigationGestures = false
        web.setValue(false, forKey: "drawsBackground")

        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1440, height: 900),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable, .fullSizeContentView],
                          backing: .buffered, defer: false)
        window.title = "Hell Grind"
        window.minSize = NSSize(width: 900, height: 600)
        window.backgroundColor = NSColor(red: 0x19 / 255.0, green: 0x19 / 255.0, blue: 0x1c / 255.0, alpha: 1)
        window.contentView = web
        window.setFrameAutosaveName("HellgrindMain")
        if !window.setFrameUsingName("HellgrindMain") { window.center() }
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)

        flushTimer = Timer.scheduledTimer(withTimeInterval: 0.25, repeats: true) { [weak self] _ in self?.flushLog() }
        showLoading()
        DispatchQueue.main.async { self.chooseStackIfNeeded() }
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ s: NSApplication) -> Bool { true }

    func applicationShouldTerminate(_ s: NSApplication) -> NSApplication.TerminateReply {
        job?.process.terminate()
        guard Prefs.stopOnQuit, !isRemote, Docker.binary != nil else { return .terminateNow }
        // Servisleri durdur, sonra çık (veri birimlerde kalır).
        let p = Process()
        p.executableURL = URL(fileURLWithPath: Docker.binary!)
        p.arguments = Docker.composeArgs(["stop"])
        p.currentDirectoryURL = Stack.dir
        p.environment = Docker.env
        p.terminationHandler = { _ in DispatchQueue.main.async { NSApp.reply(toApplicationShouldTerminate: true) } }
        do { try p.run(); return .terminateLater } catch { return .terminateNow }
    }

    // MARK: Menü

    func buildMenu() {
        let main = NSMenu()
        func sub(_ title: String, _ items: [NSMenuItem]) {
            let m = NSMenu(title: title)
            items.forEach { m.addItem($0) }
            let it = NSMenuItem(title: title, action: nil, keyEquivalent: "")
            it.submenu = m
            main.addItem(it)
        }
        func item(_ t: String, _ a: Selector?, _ k: String = "", _ mods: NSEvent.ModifierFlags = .command) -> NSMenuItem {
            let i = NSMenuItem(title: t, action: a, keyEquivalent: k)
            i.keyEquivalentModifierMask = mods
            return i
        }
        let stop = item("Çıkarken servisleri durdur", #selector(toggleStopOnQuit))
        stop.state = Prefs.stopOnQuit ? .on : .off
        stopMenuItem = stop
        sub("Hell Grind", [
            item("Hell Grind Hakkında", #selector(NSApplication.orderFrontStandardAboutPanel(_:))),
            .separator(),
            stop,
            .separator(),
            item("Hell Grind'i Gizle", #selector(NSApplication.hide(_:)), "h"),
            item("Diğerlerini Gizle", #selector(NSApplication.hideOtherApplications(_:)), "h", [.command, .option]),
            .separator(),
            item("Hell Grind'den Çık", #selector(NSApplication.terminate(_:)), "q"),
        ])
        // Panelin metin alanlarında kopyala/yapıştır için Düzen menüsü şart.
        sub("Düzen", [
            item("Geri Al", Selector(("undo:")), "z"),
            item("Yinele", Selector(("redo:")), "z", [.command, .shift]),
            .separator(),
            item("Kes", #selector(NSText.cut(_:)), "x"),
            item("Kopyala", #selector(NSText.copy(_:)), "c"),
            item("Yapıştır", #selector(NSText.paste(_:)), "v"),
            item("Tümünü Seç", #selector(NSText.selectAll(_:)), "a"),
        ])
        sub("Hat", [
            item("Paneli Yenile", #selector(reloadPanel), "r"),
            item("Tarayıcıda Aç", #selector(openInBrowser), "b", [.command, .shift]),
            .separator(),
            item("Hattı Yeniden Kur ve Başlat", #selector(restartStack), "r", [.command, .shift]),
            item("Servisleri Durdur", #selector(stopStack)),
            item("Günlükler", #selector(showLogs), "l", [.command, .shift]),
            .separator(),
            item("Hat Klasörünü Finder'da Göster", #selector(revealStack)),
            item(".env Dosyasını Aç", #selector(openEnv)),
            item("Hat Klasörünü Değiştir…", #selector(changeStack)),
            .separator(),
            item("Sunucuya Bağlan…", #selector(connectServer)),
            item("Bu Mac'te Çalıştır", #selector(useLocal)),
        ])
        let win = NSMenu(title: "Pencere")
        win.addItem(item("Küçült", #selector(NSWindow.performMiniaturize(_:)), "m"))
        win.addItem(item("Tam Ekran", #selector(NSWindow.toggleFullScreen(_:)), "f", [.command, .control]))
        let winItem = NSMenuItem(title: "Pencere", action: nil, keyEquivalent: "")
        winItem.submenu = win
        main.addItem(winItem)
        NSApp.windowsMenu = win
        NSApp.mainMenu = main
    }

    @objc func toggleStopOnQuit() {
        Prefs.stopOnQuit.toggle()
        stopMenuItem?.state = Prefs.stopOnQuit ? .on : .off
    }

    @objc func reloadPanel() { if onPanel { web.reload() } else { startStack() } }
    @objc func openInBrowser() { NSWorkspace.shared.open(panelURL) }
    @objc func restartStack() { startStack() }
    @objc func revealStack() { NSWorkspace.shared.activateFileViewerSelecting([Stack.dir]) }
    @objc func openEnv() {
        let env = Stack.dir.appendingPathComponent(".env")
        if FileManager.default.fileExists(atPath: env.path) {
            NSWorkspace.shared.open([env], withApplicationAt: URL(fileURLWithPath: "/System/Applications/TextEdit.app"),
                                    configuration: NSWorkspace.OpenConfiguration())
        } else { revealStack() }
    }

    @objc func stopStack() {
        guard !isRemote, let bin = Docker.binary else { return }
        onPanel = false
        showLoading()
        setStatus("Servisler durduruluyor…", "Verin (veritabanı, MinIO) Docker birimlerinde kalır.")
        run(bin, Docker.composeArgs(["stop"])) { [weak self] code in
            self?.setStatus(code == 0 ? "Servisler durduruldu." : "Servisler durdurulamadı.",
                            "Yeniden açmak için Başlat'a bas.", mode: code == 0 ? "idle" : "err",
                            buttons: [("retry", "Başlat"), ("logs", "Günlükler")])
        }
    }

    @objc func connectServer() { if askServer() { startStack() } }
    @objc func useLocal() {
        Prefs.serverURL = nil
        if !Prefs.chosen { chooseStackIfNeeded() } else { startStack() }
    }

    /// Uzak sunucu adresini sorar ve /healthz ile sınar.
    func askServer() -> Bool {
        let a = NSAlert()
        a.messageText = "Sunucuya bağlan"
        a.informativeText = "Hell Grind'in çalıştığı bilgisayarın adresini yaz. Ör. Tailscale ile: http://enes-mac:8000\n\n"
            + "Bu kipte bu Mac'te Docker gerekmez; projeler, ayarlar ve bütçe sunucudadır. Panelde kullanıcı girişi "
            + "yoktur: adresi yalnızca güvendiğin kişilerle, özel bir ağ (Tailscale) üzerinden paylaş."
        let field = NSTextField(frame: NSRect(x: 0, y: 0, width: 340, height: 24))
        field.placeholderString = "http://sunucu-adi:8000"
        field.stringValue = Prefs.serverURL ?? ""
        a.accessoryView = field
        a.addButton(withTitle: "Bağlan")
        a.addButton(withTitle: "Vazgeç")
        a.window.initialFirstResponder = field
        guard a.runModal() == .alertFirstButtonReturn else { return false }
        var v = field.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        while v.hasSuffix("/") { v.removeLast() }
        if v.hasSuffix("/panel") { v.removeLast(6) }
        if !v.isEmpty, !v.contains("://") { v = "http://" + v }
        if !v.isEmpty, URL(string: v)?.host != nil, let u = URL(string: v), u.port == nil, u.scheme == "http" { v += ":8000" }
        guard !v.isEmpty, let u = URL(string: v), u.host != nil, ["http", "https"].contains(u.scheme ?? "") else {
            alert("Adres geçersiz", "Ör. http://enes-mac:8000")
            return false
        }
        Prefs.serverURL = v
        Prefs.chosen = true
        return true
    }

    @objc func changeStack() {
        Prefs.serverURL = nil
        let a = NSAlert()
        a.messageText = "Hattı hangi klasörden çalıştırayım?"
        a.informativeText = "Şu an: \(Stack.dir.path)\n\nKendi repo klasörün (git ile güncellediğin) seçilirse uygulama onu kullanır. "
            + "İki yol da aynı Docker verisini (projeler, videolar) kullanır."
        a.addButton(withTitle: "Repo klasörünü seç…")
        a.addButton(withTitle: "Uygulamanın kopyasını kullan")
        a.addButton(withTitle: "Vazgeç")
        switch a.runModal() {
        case .alertFirstButtonReturn: if pickFolder() { startStack() }
        case .alertSecondButtonReturn: Prefs.stackPath = nil; Prefs.chosen = true; startStack()
        default: break
        }
    }

    // MARK: Hat klasörü seçimi (ilk açılış)

    func chooseStackIfNeeded() {
        if Prefs.chosen, !Stack.usesOwnCopy, !Stack.looksLikeRepo(Stack.dir) {
            Prefs.stackPath = nil; Prefs.chosen = false   // seçilen klasör taşınmış ya da silinmiş
        }
        guard !Prefs.chosen else { startStack(); return }
        let w = NSAlert()
        w.messageText = "Hell Grind'e hoş geldin"
        w.informativeText = "Hattı bu Mac'te mi çalıştırayım, yoksa başka bir bilgisayarda çalışan Hell Grind'e mi bağlanayım?"
            + "\n\nBu Mac'te: Docker Desktop gerekir (ücretsiz), projeler ve bütçe bu Mac'te durur.\n"
            + "Sunucuya bağlan: Docker gerekmez, sunucudaki projeler kullanılır."
        w.addButton(withTitle: "Bu Mac'te çalıştır")
        w.addButton(withTitle: "Sunucuya bağlan…")
        if w.runModal() == .alertSecondButtonReturn {
            if askServer() { startStack(); return }
        }
        let a = NSAlert()
        a.messageText = "Hat klasörü"
        a.informativeText = "Bilgisayarında hellgrind repo klasörü varsa (make up ile çalıştırdığın) onu seçebilirsin; "
            + ".env dosyan ve ayarların aynen kullanılır.\n\nYoksa uygulama kendi kopyasını kurar. İki yol da aynı Docker "
            + "verisini kullanır, mevcut projelerin kaybolmaz."
        a.addButton(withTitle: "Repo klasörünü seç…")
        a.addButton(withTitle: "Uygulamanın kopyasını kullan")
        if a.runModal() == .alertFirstButtonReturn, pickFolder() { startStack(); return }
        Prefs.stackPath = nil
        Prefs.chosen = true
        startStack()
    }

    func pickFolder() -> Bool {
        let p = NSOpenPanel()
        p.canChooseDirectories = true
        p.canChooseFiles = false
        p.allowsMultipleSelection = false
        p.message = "hellgrind repo klasörünü seç (içinde docker-compose.yml olan)"
        p.prompt = "Seç"
        guard p.runModal() == .OK, let url = p.url else { return false }
        guard Stack.looksLikeRepo(url) else {
            let a = NSAlert()
            a.messageText = "Bu klasör hellgrind reposu gibi görünmüyor."
            a.informativeText = "İçinde docker-compose.yml ve backend klasörü olmalı."
            a.runModal()
            return false
        }
        Prefs.stackPath = url.path
        Prefs.chosen = true
        return true
    }

    // MARK: Başlatma akışı

    func showLoading() {
        onPanel = false
        pageReady = false
        web.loadHTMLString(loadingHTML, baseURL: nil)
    }

    func startStack() {
        guard !starting, job == nil else { return }
        starting = true
        if onPanel { showLoading() }
        // Bekleme sayfası yüklenince devam et.
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.4) { self.stepInstall() }
    }

    func stepInstall() {
        if isRemote {
            log("Sunucu: \(apiBase)")
            return stepHealth()
        }
        setStatus("Hat dosyaları hazırlanıyor…", Stack.dir.path)
        DispatchQueue.global().async {
            var failure: String?
            do { try Stack.installOwnCopyIfNeeded { m in DispatchQueue.main.async { self.log(m) } } }
            catch { failure = error.localizedDescription }
            DispatchQueue.main.async {
                if let f = failure { return self.fail("Hat dosyaları kurulamadı.", f) }
                if !Stack.looksLikeRepo(Stack.dir) {
                    return self.fail("Hat klasörü bulunamadı.", Stack.dir.path, [("change", "Klasörü değiştir")])
                }
                Stack.ensureEnv { self.log($0) }
                self.stepDocker()
            }
        }
    }

    func stepDocker(waited: Int = 0) {
        guard Docker.binary != nil || Docker.desktopInstalled else {
            return fail("Docker Desktop bulunamadı.",
                        "Hell Grind'in servisleri (veritabanı, depo, üretim işçileri) Docker'da çalışır. Docker Desktop'ı kur, "
                        + "bir kez aç, sonra Yeniden dene'ye bas.",
                        [("getDocker", "Docker Desktop'ı indir"), ("retry", "Yeniden dene")])
        }
        DispatchQueue.global().async {
            let up = Docker.engineUp()
            DispatchQueue.main.async {
                if up { return self.stepCompose() }
                if waited == 0 {
                    self.log("Docker çalışmıyor; Docker Desktop açılıyor…")
                    NSWorkspace.shared.open(URL(fileURLWithPath: "/Applications/Docker.app"))
                }
                if waited >= 180 {
                    return self.fail("Docker Desktop açılmadı.",
                                     "Docker Desktop'ı elle aç, balina simgesi sabitlenince Yeniden dene'ye bas.",
                                     [("retry", "Yeniden dene"), ("openDocker", "Docker'ı aç")])
                }
                self.setStatus("Docker Desktop'ın açılması bekleniyor…", "\(waited) sn")
                DispatchQueue.main.asyncAfter(deadline: .now() + 3) { self.stepDocker(waited: waited + 3) }
            }
        }
    }

    func stepCompose() {
        guard let bin = Docker.binary else { return fail("docker komutu bulunamadı.", Docker.binDirs.joined(separator: "\n")) }
        setStatus("Servisler kuruluyor ve başlatılıyor…",
                  "İlk açılışta imajlar derlenir (panel, Python paketleri, ses ve kalite modelleri); internet hızına göre "
                  + "10–30 dakika sürebilir. Sonraki açılışlar saniyeler sürer.")
        log("$ docker compose -p \(composeProject) up -d --build   (\(Stack.dir.path))")
        run(bin, Docker.composeArgs(["up", "-d", "--build"])) { [weak self] code in
            guard let self = self else { return }
            if code != 0 {
                return self.fail("Servisler başlatılamadı (docker compose \(code)).",
                                 "Ayrıntı aşağıdaki günlükte. Port çakışması varsa (8000, 9000, 5433, 6379) onu kullanan "
                                 + "uygulamayı kapat.", [("retry", "Yeniden dene"), ("logs", "Servis günlükleri")])
            }
            self.stepHealth()
        }
    }

    func stepHealth(waited: Int = 0) {
        setStatus(isRemote ? "Sunucuya bağlanılıyor…" : "Panel açılıyor…",
                  waited > 5 ? "API bekleniyor (\(waited) sn)" + (isRemote ? ": \(apiBase)" : "; ilk açılışta veritabanı göçleri çalışır.") : "")
        var req = URLRequest(url: healthURL, cachePolicy: .reloadIgnoringLocalCacheData, timeoutInterval: 3)
        req.httpMethod = "GET"
        URLSession.shared.dataTask(with: req) { data, resp, _ in
            let ok = (resp as? HTTPURLResponse)?.statusCode == 200
            let body = data.flatMap { String(data: $0, encoding: .utf8) } ?? ""
            DispatchQueue.main.async {
                if ok {
                    self.log("API hazır: \(body)")
                    self.starting = false
                    self.onPanel = true
                    self.web.load(URLRequest(url: panelURL))
                    return
                }
                if waited >= (isRemote ? 20 : 240) {
                    if isRemote {
                        return self.fail("Sunucuya ulaşılamadı.",
                                         "\(healthURL.absoluteString) açılmadı. Sunucu bilgisayar açık mı, Hell Grind orada "
                                         + "çalışıyor mu, iki bilgisayar da Tailscale'e bağlı mı?",
                                         [("retry", "Yeniden dene"), ("server", "Adresi değiştir"), ("local", "Bu Mac'te çalıştır")])
                    }
                    return self.fail("API yanıt vermiyor.", "\(healthURL.absoluteString) açılmadı.",
                                     [("retry", "Yeniden dene"), ("logs", "Servis günlükleri")])
                }
                DispatchQueue.main.asyncAfter(deadline: .now() + 2) { self.stepHealth(waited: waited + 2) }
            }
        }.resume()
    }

    func run(_ exe: String, _ args: [String], done: @escaping (Int32) -> Void) {
        let j = Job(exe, args, cwd: Stack.dir, onLine: { [weak self] in self?.log($0) },
                    onExit: { [weak self] code in self?.job = nil; done(code) })
        job = j
        do { try j.start() } catch {
            job = nil
            log("Komut çalıştırılamadı: \(error.localizedDescription)")
            done(-1)
        }
    }

    func fail(_ title: String, _ detail: String, _ buttons: [(String, String)] = [("retry", "Yeniden dene")]) {
        starting = false
        setStatus(title, detail, mode: "err", buttons: buttons)
    }

    // MARK: Bekleme sayfası köprüsü

    func js(_ s: String) -> String {
        let data = try? JSONSerialization.data(withJSONObject: [s], options: [])
        let arr = data.flatMap { String(data: $0, encoding: .utf8) } ?? "[\"\"]"
        return String(arr.dropFirst().dropLast())
    }

    func setStatus(_ s: String, _ d: String = "", mode: String = "", buttons: [(String, String)] = []) {
        guard !onPanel else { return }
        let b = buttons.map { "[\(js($0.0)),\(js($0.1))]" }.joined(separator: ",")
        let call = "setStatus(\(js(s)),\(js(d)),\(js(mode)),[\(b)])"
        lastStatus = call
        if pageReady { web.evaluateJavaScript(call, completionHandler: nil) }
    }

    func log(_ line: String) {
        pendingLog += line + "\n"
        if let v = logView, logWindow?.isVisible == true {
            v.textStorage?.append(NSAttributedString(string: line + "\n", attributes: logAttrs))
            v.scrollToEndOfDocument(nil)
        }
    }

    func flushLog() {
        guard !pendingLog.isEmpty, pageReady, !onPanel else { return }
        let s = pendingLog
        pendingLog = ""
        if !onPanel { web.evaluateJavaScript("addLog(\(js(s)))", completionHandler: nil) }
    }

    func userContentController(_ c: WKUserContentController, didReceive m: WKScriptMessage) {
        guard !onPanel, let action = m.body as? String else { return }   // yalnızca bekleme sayfasından
        switch action {
        case "retry": startStack()
        case "change": changeStack()
        case "server": connectServer()
        case "local": useLocal()
        case "logs": showLogs()
        case "openDocker": NSWorkspace.shared.open(URL(fileURLWithPath: "/Applications/Docker.app"))
        case "getDocker": NSWorkspace.shared.open(URL(string: "https://www.docker.com/products/docker-desktop/")!)
        default: break
        }
    }

    // MARK: Günlük penceresi

    let logAttrs: [NSAttributedString.Key: Any] = [
        .font: NSFont.monospacedSystemFont(ofSize: 11, weight: .regular),
        .foregroundColor: NSColor(white: 0.8, alpha: 1),
    ]

    @objc func showLogs() {
        if logWindow == nil {
            let w = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 960, height: 560),
                             styleMask: [.titled, .closable, .resizable, .miniaturizable], backing: .buffered, defer: false)
            w.title = "Hell Grind — servis günlükleri"
            w.isReleasedWhenClosed = false
            let scroll = NSTextView.scrollableTextView()
            let tv = scroll.documentView as! NSTextView
            tv.isEditable = false
            tv.backgroundColor = NSColor(red: 0x23 / 255.0, green: 0x23 / 255.0, blue: 0x27 / 255.0, alpha: 1)
            w.contentView = scroll
            w.center()
            logWindow = w
            logView = tv
        }
        logView?.string = ""
        logWindow?.makeKeyAndOrderFront(nil)
        guard let bin = Docker.binary else { return }
        // Son 300 satır; canlı izleme için Terminal'de `make logs`.
        let j = Job(bin, Docker.composeArgs(["logs", "--no-color", "--tail", "300", "api", "worker", "worker-gen"]),
                    cwd: Stack.dir,
                    onLine: { [weak self] line in
                        guard let v = self?.logView, let attrs = self?.logAttrs else { return }
                        v.textStorage?.append(NSAttributedString(string: line + "\n", attributes: attrs))
                        v.scrollToEndOfDocument(nil)
                    }, onExit: { _ in })
        try? j.start()
    }

    // MARK: WebKit: gezinme, yeni pencere, indirme, dosya seçme, uyarılar

    func webView(_ w: WKWebView, decidePolicyFor a: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        if #available(macOS 11.3, *), a.shouldPerformDownload { return decisionHandler(.download) }
        if let url = a.request.url, let host = url.host,
           !["localhost", "127.0.0.1", URL(string: apiBase)?.host ?? ""].contains(host),
           a.navigationType == .linkActivated {
            NSWorkspace.shared.open(url)   // dış bağlantılar (servis panelleri, model sayfaları) tarayıcıda
            return decisionHandler(.cancel)
        }
        decisionHandler(.allow)
    }

    func webView(_ w: WKWebView, decidePolicyFor r: WKNavigationResponse,
                 decisionHandler: @escaping (WKNavigationResponsePolicy) -> Void) {
        let disp = (r.response as? HTTPURLResponse)?.value(forHTTPHeaderField: "Content-Disposition") ?? ""
        if #available(macOS 11.3, *), !r.canShowMIMEType || disp.lowercased().hasPrefix("attachment") {
            return decisionHandler(.download)
        }
        decisionHandler(.allow)
    }

    @available(macOS 11.3, *)
    func webView(_ w: WKWebView, navigationAction: WKNavigationAction, didBecome download: WKDownload) {
        download.delegate = self
    }

    @available(macOS 11.3, *)
    func webView(_ w: WKWebView, navigationResponse: WKNavigationResponse, didBecome download: WKDownload) {
        download.delegate = self
    }

    @available(macOS 11.3, *)
    func download(_ d: WKDownload, decideDestinationUsing r: URLResponse, suggestedFilename: String,
                  completionHandler: @escaping (URL?) -> Void) {
        let dir = FileManager.default.urls(for: .downloadsDirectory, in: .userDomainMask)[0]
        let name = suggestedFilename.isEmpty ? "hellgrind-indirme" : suggestedFilename
        let base = (name as NSString).deletingPathExtension, ext = (name as NSString).pathExtension
        var url = dir.appendingPathComponent(name)
        var i = 2
        while FileManager.default.fileExists(atPath: url.path) {
            url = dir.appendingPathComponent(ext.isEmpty ? "\(base) (\(i))" : "\(base) (\(i)).\(ext)")
            i += 1
        }
        completionHandler(url)
    }

    @available(macOS 11.3, *)
    func downloadDidFinish(_ d: WKDownload) {
        // İndirilen dosyayı Dock'taki İndirilenler yığınında zıplat.
        if let url = d.progress.fileURL {
            DistributedNotificationCenter.default().post(name: .init("com.apple.DownloadFileFinished"), object: url.path)
        }
    }

    @available(macOS 11.3, *)
    func download(_ d: WKDownload, didFailWithError e: Error, resumeData: Data?) {
        alert("İndirme başarısız", e.localizedDescription)
    }

    func webView(_ w: WKWebView, createWebViewWith c: WKWebViewConfiguration, for a: WKNavigationAction,
                 windowFeatures f: WKWindowFeatures) -> WKWebView? {
        if let url = a.request.url { NSWorkspace.shared.open(url) }   // "yeni sekmede aç": varsayılan tarayıcı
        return nil
    }

    func webView(_ w: WKWebView, runOpenPanelWith p: WKOpenPanelParameters, initiatedByFrame f: WKFrameInfo,
                 completionHandler: @escaping ([URL]?) -> Void) {
        let panel = NSOpenPanel()
        panel.canChooseFiles = true
        panel.canChooseDirectories = p.allowsDirectories
        panel.allowsMultipleSelection = p.allowsMultipleSelection
        panel.beginSheetModal(for: window) { r in completionHandler(r == .OK ? panel.urls : nil) }
    }

    func webView(_ w: WKWebView, runJavaScriptAlertPanelWithMessage m: String, initiatedByFrame f: WKFrameInfo,
                 completionHandler: @escaping () -> Void) {
        let a = NSAlert()
        a.messageText = m
        a.addButton(withTitle: "Tamam")
        a.beginSheetModal(for: window) { _ in completionHandler() }
    }

    func webView(_ w: WKWebView, runJavaScriptConfirmPanelWithMessage m: String, initiatedByFrame f: WKFrameInfo,
                 completionHandler: @escaping (Bool) -> Void) {
        let a = NSAlert()
        a.messageText = m
        a.addButton(withTitle: "Tamam")
        a.addButton(withTitle: "Vazgeç")
        a.beginSheetModal(for: window) { r in completionHandler(r == .alertFirstButtonReturn) }
    }

    func webView(_ w: WKWebView, runJavaScriptTextInputPanelWithPrompt p: String, defaultText: String?,
                 initiatedByFrame f: WKFrameInfo, completionHandler: @escaping (String?) -> Void) {
        let a = NSAlert()
        a.messageText = p
        let field = NSTextField(frame: NSRect(x: 0, y: 0, width: 320, height: 24))
        field.stringValue = defaultText ?? ""
        a.accessoryView = field
        a.addButton(withTitle: "Tamam")
        a.addButton(withTitle: "Vazgeç")
        a.window.initialFirstResponder = field
        a.beginSheetModal(for: window) { r in completionHandler(r == .alertFirstButtonReturn ? field.stringValue : nil) }
    }

    func webView(_ w: WKWebView, didFinish n: WKNavigation!) {
        guard !onPanel else { return }
        pageReady = true
        if let c = lastStatus { w.evaluateJavaScript(c, completionHandler: nil) }
        flushLog()
    }

    func webView(_ w: WKWebView, didFail n: WKNavigation!, withError e: Error) { panelLoadFailed(e) }
    func webView(_ w: WKWebView, didFailProvisionalNavigation n: WKNavigation!, withError e: Error) { panelLoadFailed(e) }

    func panelLoadFailed(_ e: Error) {
        let ns = e as NSError
        // İndirmeye dönüşen gezinmeler ve iptaller hata değildir.
        if ns.domain == NSURLErrorDomain && ns.code == NSURLErrorCancelled { return }
        if ns.domain == "WebKitErrorDomain" && (ns.code == 102 || ns.code == 204) { return }
        guard onPanel else { return }
        showLoading()
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.4) {
            self.fail("Panel açılamadı.", e.localizedDescription,
                      [("retry", "Hattı başlat"), ("logs", "Servis günlükleri")])
        }
    }

    func webViewWebContentProcessDidTerminate(_ w: WKWebView) { if onPanel { w.reload() } }

    func alert(_ t: String, _ m: String) {
        let a = NSAlert()
        a.messageText = t
        a.informativeText = m
        a.beginSheetModal(for: window, completionHandler: nil)
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.run()
