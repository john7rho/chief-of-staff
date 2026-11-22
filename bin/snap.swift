// Minimal webcam snapshot tool.
// usage: snap --list
//        snap [--device "<name substring>"] [--warmup 2.0] <out.jpg>
import AVFoundation
import AppKit

func devices() -> [AVCaptureDevice] {
    AVCaptureDevice.DiscoverySession(deviceTypes: [.external, .builtInWideAngleCamera],
                                     mediaType: .video, position: .unspecified).devices
}

var args = Array(CommandLine.arguments.dropFirst())
if args.first == "--list" {
    for d in devices() { print(d.localizedName) }
    exit(0)
}
var want = ""  // substring of camera name; empty = first external camera
var warmup = 2.0
var out = ""
while !args.isEmpty {
    let a = args.removeFirst()
    if a == "--device", !args.isEmpty { want = args.removeFirst() }
    else if a == "--warmup", !args.isEmpty { warmup = Double(args.removeFirst()) ?? 2.0 }
    else { out = a }
}
guard !out.isEmpty else { fputs("usage: snap [--device name] [--warmup s] out.jpg\n", stderr); exit(2) }

let sema = DispatchSemaphore(value: 0)
var granted = false
AVCaptureDevice.requestAccess(for: .video) { granted = $0; sema.signal() }
sema.wait()
guard granted else { fputs("camera permission denied\n", stderr); exit(3) }

let all = devices()
let ext = all.first(where: { $0.deviceType == .external })
guard let dev = (want.isEmpty ? nil : all.first(where: { $0.localizedName.localizedCaseInsensitiveContains(want) })) ?? ext ?? all.first else {
    fputs("no camera found\n", stderr); exit(4)
}

final class Grabber: NSObject, AVCaptureVideoDataOutputSampleBufferDelegate {
    var latest: CVImageBuffer?
    let q = DispatchQueue(label: "frames")
    func captureOutput(_ o: AVCaptureOutput, didOutput sb: CMSampleBuffer, from c: AVCaptureConnection) {
        latest = CMSampleBufferGetImageBuffer(sb)
    }
}

let session = AVCaptureSession()
session.sessionPreset = .high
let input = try AVCaptureDeviceInput(device: dev)
session.addInput(input)
let output = AVCaptureVideoDataOutput()
output.videoSettings = [kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA]
let g = Grabber()
output.setSampleBufferDelegate(g, queue: g.q)
session.addOutput(output)
session.startRunning()
Thread.sleep(forTimeInterval: warmup)  // let auto-exposure / white balance settle
var buf: CVImageBuffer?
g.q.sync { buf = g.latest }
session.stopRunning()
guard let b = buf else { fputs("no frame captured\n", stderr); exit(5) }
let ci = CIImage(cvImageBuffer: b)
let rep = NSBitmapImageRep(ciImage: ci)
guard let jpg = rep.representation(using: .jpeg, properties: [.compressionFactor: 0.85]) else { exit(6) }
try jpg.write(to: URL(fileURLWithPath: out))
print("\(dev.localizedName) -> \(out)")
