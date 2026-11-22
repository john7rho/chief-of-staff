// BedJet 3 Bluetooth LE controller.
// usage: BedJetCtl --out <result.txt> <command> [arg]
// commands: scan | status | off | cool | heat | turbo | dry | ext-heat | temp <°F> | fan <5-100> | runtime <H:MM>
// Protocol per the open-source Home Assistant BedJet integration.
import CoreBluetooth
import Foundation

let SERVICE = CBUUID(string: "00001000-bed0-0080-aa55-4265644a6574")
let STATUS = CBUUID(string: "00002000-bed0-0080-aa55-4265644a6574")
let COMMAND = CBUUID(string: "00002004-bed0-0080-aa55-4265644a6574")
let CACHE = NSString(string: "~/ChiefOfStaff/.bedjet_id").expandingTildeInPath

var args = Array(CommandLine.arguments.dropFirst())
var outPath = "/dev/stdout"
if let i = args.firstIndex(of: "--out"), i + 1 < args.count { outPath = args[i + 1]; args.removeSubrange(i...i + 1) }
let cmd = args.first ?? "status"
let arg = args.count > 1 ? args[1] : ""

func finish(_ msg: String, _ code: Int32) -> Never {
    try? (msg + "\n").write(toFile: outPath, atomically: true, encoding: .utf8)
    exit(code)
}

func payload() -> [UInt8]? {
    switch cmd {
    case "off": return [0x01, 0x01]
    case "cool": return [0x01, 0x02]
    case "heat": return [0x01, 0x03]
    case "turbo": return [0x01, 0x04]
    case "dry": return [0x01, 0x05]
    case "ext-heat": return [0x01, 0x06]
    case "temp":
        guard let f = Double(arg) else { finish("temp needs °F", 2) }
        let c = (f - 32) * 5 / 9
        return [0x03, UInt8(max(19, min(109, Int((c * 2).rounded()))))]   // ~49–122°F, half-degree C units
    case "fan":
        guard let p = Int(arg) else { finish("fan needs 5-100", 2) }
        return [0x07, UInt8(max(0, min(19, p / 5 - 1)))]
    case "runtime":
        let hm = arg.split(separator: ":").compactMap { UInt8($0) }
        guard hm.count == 2 else { finish("runtime needs H:MM", 2) }
        return [0x02, hm[0], hm[1]]
    default: return nil
    }
}

final class BJ: NSObject, CBCentralManagerDelegate, CBPeripheralDelegate {
    var cm: CBCentralManager!
    var target: CBPeripheral?
    var found: [String] = []
    let data = payload()

    override init() { super.init(); cm = CBCentralManager(delegate: self, queue: nil) }

    func centralManagerDidUpdateState(_ c: CBCentralManager) {
        switch c.state {
        case .poweredOn:
            if !cmd.hasPrefix("scan"), let s = try? String(contentsOfFile: CACHE, encoding: .utf8),
               let u = UUID(uuidString: s.trimmingCharacters(in: .whitespacesAndNewlines)),
               let p = c.retrievePeripherals(withIdentifiers: [u]).first {
                connect(p); return
            }
            c.scanForPeripherals(withServices: nil, options: nil)
            DispatchQueue.main.asyncAfter(deadline: .now() + (cmd.hasPrefix("scan") ? 10 : 15)) {
                if cmd.hasPrefix("scan") { finish(self.found.isEmpty ? "no BedJet found" : self.found.joined(separator: "\n"), self.found.isEmpty ? 1 : 0) }
                finish("BedJet not found (in range? phone app disconnected?)", 1)
            }
        case .unauthorized: finish("bluetooth permission denied (allow BedJetCtl in System Settings > Privacy & Security > Bluetooth)", 3)
        case .poweredOff: finish("bluetooth is off", 3)
        default: break
        }
    }

    func centralManager(_ c: CBCentralManager, didDiscover p: CBPeripheral, advertisementData ad: [String: Any], rssi: NSNumber) {
        let name = (ad[CBAdvertisementDataLocalNameKey] as? String) ?? p.name ?? ""
        let svcs = (ad[CBAdvertisementDataServiceUUIDsKey] as? [CBUUID]) ?? []
        if cmd == "scan-all" {
            let line = "\(name.isEmpty ? "(no name)" : name)  rssi=\(rssi)  svcs=\(svcs.map { $0.uuidString }.joined(separator: ","))"
            if !name.isEmpty && !found.contains(line) { found.append(line) }
            return
        }
        guard name.uppercased().contains("BEDJET") || svcs.contains(SERVICE) else { return }
        if cmd == "scan" {
            let line = "\(name.isEmpty ? "BedJet" : name)  id=\(p.identifier.uuidString)  rssi=\(rssi)"
            if !found.contains(where: { $0.contains(p.identifier.uuidString) }) { found.append(line) }
            try? p.identifier.uuidString.write(toFile: CACHE, atomically: true, encoding: .utf8)
            return
        }
        c.stopScan()
        try? p.identifier.uuidString.write(toFile: CACHE, atomically: true, encoding: .utf8)
        connect(p)
    }

    func connect(_ p: CBPeripheral) {
        target = p; p.delegate = self; cm.connect(p, options: nil)
        DispatchQueue.main.asyncAfter(deadline: .now() + 15) { finish("connect timed out (is the BedJet app connected on your phone?)", 1) }
    }

    func centralManager(_ c: CBCentralManager, didConnect p: CBPeripheral) { p.discoverServices([SERVICE]) }
    func centralManager(_ c: CBCentralManager, didFailToConnect p: CBPeripheral, error: Error?) { finish("connect failed: \(error?.localizedDescription ?? "?")", 1) }

    func peripheral(_ p: CBPeripheral, didDiscoverServices e: Error?) {
        guard let s = p.services?.first(where: { $0.uuid == SERVICE }) else { finish("BedJet service not found", 1) }
        p.discoverCharacteristics([STATUS, COMMAND], for: s)
    }

    func peripheral(_ p: CBPeripheral, didDiscoverCharacteristicsFor s: CBService, error e: Error?) {
        let chars = s.characteristics ?? []
        if cmd == "status" {
            finish("connected to \(p.name ?? "BedJet") (\(p.identifier.uuidString))", 0)
        }
        guard let c = chars.first(where: { $0.uuid == COMMAND }), let d = data else { finish("unknown command \(cmd)", 2) }
        p.writeValue(Data(d), for: c, type: .withResponse)
    }

    func peripheral(_ p: CBPeripheral, didWriteValueFor c: CBCharacteristic, error e: Error?) {
        if let e = e { finish("write failed: \(e.localizedDescription)", 1) }
        cm.cancelPeripheralConnection(p)
        finish("ok: \(cmd) \(arg)".trimmingCharacters(in: .whitespaces), 0)
    }
}

let bj = BJ()
RunLoop.main.run()
