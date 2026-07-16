#!/usr/bin/env python3
"""Generate office_ring.wav — a vintage early-electronic PBX warble ring for the WP826.
16 kHz mono 16-bit PCM (accepted by the phone's custom-ringtone upload). Upload with:
  node tools/wp826.mjs ring-upload tools/office_ring.wav
then point a Match-Incoming-Caller-ID rule ringtone (P1489) at the returned id."""
import math, wave, struct
RATE = 16000
def s16(x): return max(-32767, min(32767, int(x * 32767)))
def warble(dur, flo, fhi, whz, amp=0.72):
    n = int(dur * RATE); out = []; ph = 0.0; edge = int(0.006 * RATE)
    for i in range(n):
        t = i / RATE; f = fhi if math.sin(2*math.pi*whz*t) >= 0 else flo; ph += 2*math.pi*f/RATE
        env = 1.0
        if i < edge: env = i/edge
        elif i > n-edge: env = (n-i)/edge
        out.append(s16(math.sin(ph)*amp*env))
    return out
def sil(d): return [0]*int(d*RATE)
A = warble(0.40,1046,1318,18)+sil(0.18)+warble(0.40,1046,1318,18)+sil(1.60)  # Nortel/Meridian-style double-warble
with wave.open("office_ring.wav","wb") as w:
    w.setnchannels(1); w.setsampwidth(2); w.setframerate(RATE)
    w.writeframes(b''.join(struct.pack('<h', s) for s in A))
print("wrote office_ring.wav")
