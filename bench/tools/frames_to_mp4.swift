// Arma un MP4 (H.264, 30 fps) a partir de frames.json: cada instante muestra el último frame recibido.
// uso: swift mkvideo.swift <dir con frames.json> <salida.mp4> [fin extra en s]
import AVFoundation
import AppKit
import Foundation

let dir = URL(fileURLWithPath: CommandLine.arguments[1])
let out = URL(fileURLWithPath: CommandLine.arguments[2])
let tail = CommandLine.arguments.count > 3 ? Double(CommandLine.arguments[3])! : 1.0
struct F: Decodable { let f: String; let t: Double }
struct Meta: Decodable { let width: Int; let height: Int; let frames: [F] }
let meta = try JSONDecoder().decode(Meta.self, from: Data(contentsOf: dir.appendingPathComponent("frames.json")))
try? FileManager.default.removeItem(at: out)
let w = try AVAssetWriter(outputURL: out, fileType: .mp4)
let settings: [String: Any] = [AVVideoCodecKey: AVVideoCodecType.h264, AVVideoWidthKey: meta.width,
                               AVVideoHeightKey: meta.height,
                               AVVideoCompressionPropertiesKey: [AVVideoAverageBitRateKey: 6_000_000]]
let input = AVAssetWriterInput(mediaType: .video, outputSettings: settings)
let adaptor = AVAssetWriterInputPixelBufferAdaptor(assetWriterInput: input, sourcePixelBufferAttributes: [
    kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32ARGB,
    kCVPixelBufferWidthKey as String: meta.width, kCVPixelBufferHeightKey as String: meta.height])
w.add(input)
w.startWriting()
w.startSession(atSourceTime: .zero)

func buffer(_ name: String) -> CVPixelBuffer {
    let img = NSImage(contentsOf: dir.appendingPathComponent("frames").appendingPathComponent(name))!
    var pb: CVPixelBuffer?
    CVPixelBufferCreate(nil, meta.width, meta.height, kCVPixelFormatType_32ARGB, nil, &pb)
    CVPixelBufferLockBaseAddress(pb!, [])
    let ctx = CGContext(data: CVPixelBufferGetBaseAddress(pb!), width: meta.width, height: meta.height,
                        bitsPerComponent: 8, bytesPerRow: CVPixelBufferGetBytesPerRow(pb!),
                        space: CGColorSpaceCreateDeviceRGB(), bitmapInfo: CGImageAlphaInfo.noneSkipFirst.rawValue)!
    var rect = CGRect(x: 0, y: 0, width: meta.width, height: meta.height)
    ctx.draw(img.cgImage(forProposedRect: &rect, context: nil, hints: nil)!, in: rect)
    CVPixelBufferUnlockBaseAddress(pb!, [])
    return pb!
}

let fps = 30.0
let total = meta.frames.last!.t + tail
var k = 0
var cache: (String, CVPixelBuffer)? = nil
var i = 0
while Double(i) / fps <= total {
    let t = Double(i) / fps
    while k + 1 < meta.frames.count && meta.frames[k + 1].t <= t { k += 1 }
    let name = meta.frames[k].f
    if cache?.0 != name { cache = (name, buffer(name)) }
    while !input.isReadyForMoreMediaData { usleep(1000) }
    adaptor.append(cache!.1, withPresentationTime: CMTime(value: CMTimeValue(i), timescale: CMTimeScale(fps)))
    i += 1
}
input.markAsFinished()
let sem = DispatchSemaphore(value: 0)
w.finishWriting { sem.signal() }
sem.wait()
print("\(out.path): \(i) frames · \(String(format: "%.1f", total)) s · \(meta.width)x\(meta.height) · \(w.status == .completed ? "OK" : "ERROR \(String(describing: w.error))")")
