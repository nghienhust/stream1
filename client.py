import gi
import time
import socket
import struct
import threading
from collections import deque

gi.require_version('Gst', '1.0')
from gi.repository import Gst, GLib

Gst.init(None)

VIDEO_PORT = 5000
META_PORT  = 5001

# ── FIFO queue chứa send_ts từ Pi ────────────────────────
# Server gửi 1 entry / frame, client pop 1 / frame tại c2
# Dùng deque thay list để popleft() O(1)
send_ts_queue = deque(maxlen=150)   #buffer ~5s ở 30fps
queue_lock    = threading.Lock()

def meta_receiver():
    """Nhận (seq, send_ts) từ Pi, push send_ts vào FIFO."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(('0.0.0.0', META_PORT))
    sock.settimeout(1.0)
    print(f"[META] Listening UDP:{META_PORT}")
    while True:
        try:
            data, _ = sock.recvfrom(64)
            if len(data) >= 16:
                _seq, send_ts = struct.unpack('>QQ', data[:16])
                with queue_lock:
                    send_ts_queue.append(send_ts)
        except socket.timeout:
            continue
        except Exception as e:
            print(f"[META] Error: {e}")

threading.Thread(target=meta_receiver, daemon=True).start()

#Stage timing: key = local recv_seq (đếm tại c2)
c_stage = {}
c2_seq  = 0   #đếm tại c2 để track từng H264 frame

jitter_delays = deque(maxlen=60)
depay_delays  = deque(maxlen=60)
decode_delays = deque(maxlen=60)
rx_delays     = deque(maxlen=60)
e2e_delays    = deque(maxlen=60)

frame_count = 0   
byte_count  = 0
start_time  = time.time()

pipeline_str = f"""
udpsrc port={VIDEO_PORT} caps="application/x-rtp,media=video,encoding-name=H264,payload=96" !
identity name=c0 !
rtpjitterbuffer latency=30 !
identity name=c1 !
rtph264depay !
identity name=c2 !
avdec_h264 !
identity name=c3 !
autovideosink sync=false
"""
pipeline = Gst.parse_launch(pipeline_str)
c0 = pipeline.get_by_name("c0")   # sau udpsrc, RTP packet đến
c1 = pipeline.get_by_name("c1")   # sau jitterbuf, packet released 
c2 = pipeline.get_by_name("c2")   # sau depay, 1 H264 NAL / frame ← pop FIFO đây
c3 = pipeline.get_by_name("c3")   # sau avdec, 1 raw frame, tính fps

#c0: ghi r0 = lúc RTP packet đến
#Mỗi H264 frame có thể có nhiều RTP packet, c0 probe nhiều lần / frame
#Chỉ ghi r0 lần đầu tiên cho mỗi nhóm packet (dùng c2_seq làm group key)
c0_last_c2_seq = -1
def probe_c0(pad, info):
    global c0_last_c2_seq
    if not info.get_buffer():
        return Gst.PadProbeReturn.OK
    # c2_seq chưa tăng nên vẫn là seq của frame đang đến
    seq = c2_seq
    if seq not in c_stage:
        c_stage[seq] = {"r0": time.time_ns()}
    # Không ghi đè r0 nếu đã có (chỉ lấy lần đầu)
    return Gst.PadProbeReturn.OK
#c1: ghi r1 = sau jitterbuffer
def probe_c1(pad, info):
    if not info.get_buffer():
        return Gst.PadProbeReturn.OK
    seq = c2_seq
    if seq in c_stage and "r1" not in c_stage[seq]:
        c_stage[seq]["r1"] = time.time_ns()
    return Gst.PadProbeReturn.OK
#c2: ghi r2, pop send_ts từ FIFO
#Đây là điểm đồng bộ: 1 buffer / H264 frame (depay đã reassemble)
#Server cũng gửi 1 meta / frame → FIFO 1:1
def probe_c2(pad, info):
    global c2_seq
    if not info.get_buffer():
        return Gst.PadProbeReturn.OK
    r2  = time.time_ns()
    seq = c2_seq   # frame hiện tại
    # Pop send_ts từ FIFO
    send_ts = None
    with queue_lock:
        if send_ts_queue:
            send_ts = send_ts_queue.popleft()
    # Đảm bảo entry tồn tại
    if seq not in c_stage:
        c_stage[seq] = {}
    c_stage[seq]["r2"]      = r2
    c_stage[seq]["send_ts"] = send_ts
    c2_seq += 1   # tăng sau khi xử lý xong frame này
    return Gst.PadProbeReturn.OK
#c3: ghi r3, tính delay, đếm fps
def probe_c3(pad, info):
    global frame_count, byte_count
    buf = info.get_buffer()
    if not buf:
        return Gst.PadProbeReturn.OK
    r3  = time.time_ns()
    #c2_seq đã tăng, frame vừa decode là c2_seq - 1
    seq = c2_seq - 1
    if seq in c_stage:
        d  = c_stage.pop(seq)
        r0 = d.get("r0")
        r1 = d.get("r1")
        r2 = d.get("r2")
        st = d.get("send_ts")
        if r0 and r1:
            jitter_delays.append((r1 - r0) / 1e6)
        if r1 and r2:
            depay_delays.append( (r2 - r1) / 1e6)
        if r2:
            decode_delays.append((r3 - r2) / 1e6)
        if r0:
            rx_delays.append(    (r3 - r0) / 1e6)
        if st:
            e2e_delays.append(   (r3 - st) / 1e6)
    #Dọn entry cũ hơn 5s
    cutoff = time.time_ns() - 5_000_000_000
    stale  = [k for k, v in list(c_stage.items()) if v.get("r0", cutoff+1) < cutoff]
    for k in stale:
        del c_stage[k]
    frame_count += 1          #fps: 1 raw frame = 1 increment
    byte_count  += buf.get_size()
    return Gst.PadProbeReturn.OK

c0.get_static_pad("src").add_probe(Gst.PadProbeType.BUFFER, probe_c0)
c1.get_static_pad("src").add_probe(Gst.PadProbeType.BUFFER, probe_c1)
c2.get_static_pad("src").add_probe(Gst.PadProbeType.BUFFER, probe_c2)
c3.get_static_pad("src").add_probe(Gst.PadProbeType.BUFFER, probe_c3)

def print_stats():
    global frame_count, byte_count, start_time
    elapsed = time.time() - start_time
    fps  = frame_count / elapsed if elapsed > 0 else 0
    mbps = (byte_count * 8) / elapsed / 1e6 if elapsed > 0 else 0
    avg  = lambda d: f"{sum(d)/len(d):.2f}" if d else "-.--"

    with queue_lock:
        q = len(send_ts_queue)   # nên gần 0; nếu tăng = FIFO lệch
    #network delay
    net_str = "-.--"
    if e2e_delays and rx_delays:
        net_ms  = sum(e2e_delays)/len(e2e_delays) - sum(rx_delays)/len(rx_delays)
        net_str = f"{net_ms:.2f}"

    print(
        f"[LAPTOP]  FPS:{fps:.1f}  {mbps:.2f}Mbps  (fifo:{q})\n"
        f"          jitter_buf : {avg(jitter_delays)} ms \n"
        f"          depay      : {avg(depay_delays)} ms \n"
        f"          decode     : {avg(decode_delays)} ms \n"
        f"          total_rx   : {avg(rx_delays)} ms  \n"
        f"          network    : {net_str} ms  end2end - total_rx\n"
        f"          end2end    : {avg(e2e_delays)} ms \n"
    )
    frame_count = 0
    byte_count  = 0
    start_time  = time.time()
    return True

GLib.timeout_add_seconds(1, print_stats)
pipeline.set_state(Gst.State.PLAYING)
print(f"[LAPTOP] Video UDP:{VIDEO_PORT}  |  Meta UDP:{META_PORT}")
GLib.MainLoop().run()
