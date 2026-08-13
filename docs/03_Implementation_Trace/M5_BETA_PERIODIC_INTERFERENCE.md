# M5 Beta — Periodic Interference / Audio Golden Case

## Why this increment exists

The APF1250 field case `CS20260807-6886043` exposed a blind spot in the original Hum detector: the 50 Hz fundamental is not dominant, while low-energy `pcm_rx` contains a very stable ~20 ms repetition and a 150/250/350/450/... Hz odd-harmonic comb. A detector that only asks “is 50/60 Hz hum strong?” returns a false negative.

M5 Beta therefore treats periodic interference as a first-class deterministic signal pattern rather than overloading `Hum`.

## New deterministic pipeline

```text
Active Media Window
  -> Low-energy window selection
  -> ACF 10 / 20 / 40 ms
  -> period estimation (15–25 ms search)
  -> 150/250/350/...Hz odd-50-Hz harmonic comb
  -> pcm_rx vs upstream RTP vs reverse RTP comparison
  -> LOCAL_CAPTURE_PERIODIC_INTERFERENCE
  -> Rule Engine / Hypothesis
```

### Detection boundary

A HIGH event requires, by default:

- `ACF(20 ms) >= 0.80`
- `ACF(40 ms) >= 0.75`
- `ACF(10 ms) <= -0.50`
- at least 5 prominent peaks among 150/250/.../950 Hz
- the same pattern remains clear in the correlated upstream RTP
- reverse RTP is materially weaker, when the reverse stream is available

The event means the periodic noise is already present in the local capture direction and propagates into transmitted RTP. It **does not** auto-confirm power supply, grounding, phone, line, FXS/SLIC, or PCM interface as the final hardware root cause.

## Active media scoping

SIP calls now expose:

- `media_start_time`: ACK after accepted INVITE, or accepted 2xx if ACK is missing
- `media_end_time`: BYE if present, otherwise capture end for the Dialog
- `active_media_duration_seconds`

When SIP is available, periodic analysis is clamped to this media window. In RTP-only fallback mode, the bidirectional RTP overlap is used as the media window.

## Analyzer availability semantics

Fallback parsing no longer implies “zero SIP calls”. Media output now carries availability:

```json
{
  "sip": "UNAVAILABLE",
  "sdp": "UNAVAILABLE",
  "rtp": "AVAILABLE",
  "rtcp": "UNAVAILABLE"
}
```

and top-level media `call_count` is `null`, not `0`, when SIP analysis was not available.

## Rule

New reviewed rule:

`rules/diagnosis/local_capture_periodic_interference.yaml`

It creates a `SUPPORTED` hypothesis:

`LOCAL_CAPTURE_PERIODIC_INTERFERENCE`

with confidence 0.96 for an audio-noise symptom when at least one deterministic local-capture periodic propagation event exists.

## Golden Case #1

Manifest:

`golden_cases/APF1250_CS20260807_6886043/manifest.yaml`

Replay:

```bash
PYTHONPATH=backend python tools/golden_audio_replay.py \
  /path/to/8b72929e-8a06-4f1e-a922-1d3779ebbd6f.pcap \
  --tshark /path/to/tshark
```

The manifest checks:

- at least 2 local-capture periodic events
- both effective `pcm_rx` sessions have `ACF(20ms) >= 0.95`
- both have `ACF(10ms) <= -0.80`
- upstream RTP keeps strong 20 ms periodicity
- reverse RTP is weaker
- at least 7 odd-harmonic comb hits
- diagnosis promotes `LOCAL_CAPTURE_PERIODIC_INTERFERENCE` to `SUPPORTED`
- no specific hardware root is auto-CONFIRMED

## Real sample replay in this build

Using the supplied field PCAP through the restricted RTP fallback path:

### Session 6

- pcm_rx ACF(10 ms): `-0.923528`
- pcm_rx ACF(20 ms): `+0.985749`
- pcm_rx ACF(40 ms): `+0.980073`
- upstream RTP ACF(20 ms): `+0.880720`
- reverse RTP ACF(20 ms): `+0.191794`
- comb hits: `9` (150 through 950 Hz)

### Session 7

- pcm_rx ACF(10 ms): `-0.948095`
- pcm_rx ACF(20 ms): `+0.995999`
- pcm_rx ACF(40 ms): `+0.990943`
- upstream RTP ACF(20 ms): `+0.953467`
- reverse RTP ACF(20 ms): `+0.424351`
- comb hits: `9`

Diagnosis result:

- top hypothesis: `LOCAL_CAPTURE_PERIODIC_INTERFERENCE`
- status: `SUPPORTED`
- confidence: `0.96`
- diagnosis state: `DIAGNOSED`
- specific hardware root remains unconfirmed and requests A/B validation

## Tests

This increment adds unit tests for:

- synthetic odd-50-Hz comb + 20 ms periodicity
- random-noise negative control
- cross-layer local-capture direction localization
- Diagnosis prioritization
- Rule matching
- `UNAVAILABLE != 0` SIP semantics
- SIP active media window
