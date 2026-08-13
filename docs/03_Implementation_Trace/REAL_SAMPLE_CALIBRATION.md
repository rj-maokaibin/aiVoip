# Real PCAP calibration — M2/M3

Input SHA256: `3af13c0142b5cb86a60dc0642b572261735d6af770806dcb05e70e2c574f8fbc`

This file was used only as a calibration sample. The raw PCAP is not bundled in the repository.

## Capture summary

- 35,117 frames, about 424.928 s.
- SIP/UDP 5060 is present.
- Private diagnostic PCM:
  - `dst 40000`: 13,909 packets, fixed 160-byte UDP payload, `pcm_rx`.
  - `dst 50000`: 13,909 packets, fixed 160-byte UDP payload, `pcm_tx`.
- The PCM packet median spacing is about 10 ms.
- With 160 bytes = 80 signed 16-bit samples every 10 ms, and cross-layer audio correlation, the calibrated profile is `8 kHz / signed 16-bit little-endian / mono`.

## SIP observations used to harden M2

Observed dial attempts include target `6`, `8802`, successful `8802`, and successful `8803`.
A successful INVITE leg can still be followed by a later `487` in a partial/multi-leg capture. M2 therefore no longer uses "the last INVITE status code" as the successful call's final status. It preserves the accepted 2xx and records later conflicting final responses separately.

## RTP calibration

All stable media streams in this sample use PT 8 / PCMA / 8 kHz with 160-byte RTP payloads and a 160 timestamp step, i.e. 20 ms packetization.

### Successful 8803 call, media port 17074

Device -> peer:
- packets: 1,999
- sequence loss: 0
- duplicate: 0
- out-of-order: 0
- max Delta: about 48.36 ms
- no Delta >= 60 ms
- max RFC3550 jitter: about 3.52 ms

Peer -> device:
- packets: 1,990
- sequence loss: 0
- duplicate: 0
- out-of-order: 0
- 5 arrival-Delta spikes >= 60 ms
- max Delta: about 244.02 ms
- max RFC3550 jitter: about 27.65 ms

This proves that packet loss and arrival jitter/stall must be separate anomaly classes.

### Successful 8802 call, media port 17066

Device -> peer has no Delta >= 60 ms. Peer -> device has 2 such spikes; maximum Delta is about 183.20 ms.

## PCM calibration

Both 40000 and 50000 produce 8 capture sessions in this file. The M3 parser splits sessions on >100 ms gaps instead of concatenating unrelated capture periods.

High-confidence in-band DTMF detection on `pcm_rx` yields dial-like sequences matching SIP targets, including `8802` and `8803`. The detector uses dual-tone energy, row/column dominance, twist and minimum duration to reject broad tonal noise.

For the final call, decoded PCMA RTP in the device->peer direction correlates strongly with `pcm_rx` (max correlation about 0.968, approximately 30 ms offset), validating the tap direction and PCM format.

The current hum-family detector rates the last two PCM sessions LOW for classic 50/60 Hz families. This is only a spectral observation; it does not rule out other tonal, comb-spectrum, pulse, click/pop or analog interference mechanisms.

## Resulting code changes

- PCM private UDP reader for Ethernet/VLAN + IPv4 PCAP.
- `ruijie_aim_diag_v1` PCM profile.
- PCM session splitting and format validation.
- RMS/dBFS/Peak/DC/Clipping.
- Hum-family evidence score.
- Hardened Goertzel DTMF candidates + dial-sequence grouping.
- Cross-layer PCM DTMF ↔ SIP dial target correlation API.
- Separate `pcm-worker` and PCM Analyzer Job API.
- SIP conflicting final-response handling.
- RTP p95 Delta/Jitter and high-Delta/excess-delay metrics.
- G.711A/G.711U decoder foundation.
