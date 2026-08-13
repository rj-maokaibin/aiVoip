# Phase B1 — SIP/RTP/PCM/Audio Semantic Hardening

## 1. Purpose

Phase B1 aligns the existing M6.1 analyzers with the frozen V1.0 Engineering Contract before M6.2 Reproduction Intelligence is implemented. The goal is to remove implicit protocol/audio guesses, externalize diagnosis-affecting analyzer parameters, and make analyzer runs reproducible by version/checksum.

This phase deliberately does **not** implement EC-02 real DUT command mappings and does **not** start the M6.2 reproduction state machine.

## 2. Implemented contract hardening

### 2.1 Versioned AnalyzerProfile

Added `profiles/analyzers/voip_v1.yaml` and `backend/app/analyzers/profile.py`.

The profile freezes diagnosis-affecting configuration for:

- RTP ptime/high-delta/burst/jitter/call-scope/one-way thresholds
- silence detection
- click/pop detection
- echo detection
- periodic interference and odd-50-Hz harmonic-comb detection
- spectral/narrow-band analysis
- DTMF analysis
- clipping metric threshold
- hum-family scoring
- media/PCM correlation

The loader validates schema/id/version/status and calculates a deterministic SHA-256 checksum. AnalyzerRun snapshots now persist the effective AnalyzerProfile definition and checksum.

### 2.2 RTP semantic hardening

- Unknown/dynamic payload type without an SDP mapping no longer falls back to an assumed 8 kHz clock.
- Clock rate, ptime, RFC3550 jitter and audio-loss duration remain explicitly unavailable when their prerequisites are unavailable.
- SDP `ptime` has priority over RTP timestamp-delta inference.
- RTP timestamp inference is used only when clock rate is known and the derived value falls within the frozen profile range.
- ptime output includes availability and provenance (`SDP`, `RTP_TIMESTAMP_INFERRED`, `UNAVAILABLE`).
- High-delta, burst-loss severity, jitter filter divisor, call-scope tolerance and one-way minimum-packet parameters are profile-backed.

### 2.3 RTCP semantic hardening

- RTCP packet types 200/201/202/203 normalize to SR/RR/SDES/BYE while preserving raw values.
- RTT is calculated only when LSR/DLSR are present **and** the capture timestamp is compatible with the absolute NTP/Unix-time conversion required by the calculation.
- Relative/synthetic capture time does not produce a fabricated RTT; availability remains explicit instead.

### 2.4 PCM format contract

`backend/app/analyzers/pcm/profile.py` now supports a versioned PCM Profile with checksum and explicit format status.

A PCM profile declares transport and sample semantics including:

- header length
- payload offset
- PCM payload bytes
- sample rate
- bit depth
- signedness
- endianness
- channel count
- expected packet interval/session gap

`profiles/pcm/ruijie_aim_diag_v1.yaml` is marked `VERIFIED` for the already validated APF/Ruijie AIM diagnostic sample format: 8 kHz, signed 16-bit little-endian mono, 160 PCM bytes per packet, 10 ms expected interval, RX UDP 40000 and TX UDP 50000, no extra header in this verified profile.

Unknown formats can use `RAW` mode. RAW mode preserves packet/session facts but blocks semantic audio decoding instead of guessing the format.

### 2.5 Audio analyzer hardening

The previously validated M6.1 behavior was preserved while moving material parameters into the versioned AnalyzerProfile:

- Silence: active-media-scoped adaptive noise-floor/speech thresholds and context checks.
- Click/Pop: robust sample-jump + MAD/outlier + short-time energy + high-band evidence.
- Echo: bounded cross-correlation with delay/correlation/overlap requirements.
- Periodic interference: low-energy window selection, ACF 10/20/40 ms, 15–25 ms period search, and odd 50 Hz harmonic-comb support.
- DTMF: frame/hop, dominance, twist, duration and inter-digit parameters.
- Hum/spectral/correlation helpers: profile-backed thresholds.

No new uncalibrated root-cause threshold was invented. In particular, a signal-level periodic/echo finding still cannot directly confirm a specific power-supply/SLIC/AEC hardware root cause without the required deterministic/experimental evidence.

### 2.6 AnalyzerRun reproducibility

Packet, PCM and media workers persist effective analyzer configuration snapshots/checksums with AnalyzerRun. PCM/media runs also include the effective PCM Profile snapshot.

PCM RAW-mode analysis reports `PARTIAL_SUCCESS` rather than a false `SUCCESS` when the packet evidence is usable but audio semantics are unavailable.

### 2.7 Profile validation gate

`tools/check_profiles.py` and the Makefile `profiles`/`quality-gate` targets validate:

- Action Registry/profile syntax already present in the baseline
- AnalyzerProfile schema/version/checksum
- PCM Profile schema/version/format status/checksum

## 3. Validation

Final Phase B1 validation after all changes:

- Python/migration compile: PASS
- Backend tests: **90 / 90 PASS**
- Rule validation: **11 / 11 PASS**
- Synthetic Golden: **5 / 5 cases, 21 / 21 checks PASS**
- Synthetic E2E: **10 / 10 cases, 53 / 53 checks PASS**
- Baseline diff: **0 regressions, 0 changes**
- APF1250 field Golden `CS20260807-6886043`: **15 / 15 checks PASS** (`analysis_status=PARTIAL_SUCCESS`, consistent with the existing real capture scope)
- Docker full-stack runtime: **UNVERIFIED** because the execution environment has no Docker CLI/daemon. This is not recorded as PASS or FAIL.

## 4. Intentional boundaries / remaining work

The following are intentionally not claimed as complete in B1:

1. **EC-02 remains RESERVED / PENDING_PLATFORM_CONTRACT.** No real Voice Gateway resolver command, AIM Debug ON/OFF command or OFFHOOK/ONHOOK real-time event source was invented.
2. **M6.2 is not implemented yet.** ReproductionSession/AUTO_ARMING/WATCHING/Attempt/CallQuickAnalyzer/Evidence Sufficiency/Cleanup Verification/Experiment/Fix Verification remain Phase C+ work.
3. RTCP-vs-local-RTP discrepancy-to-Contradiction automation remains a later semantic/diagnosis enhancement; B1 preserves both fact sources but does not claim a complete contradiction rule set.
4. Full call-scoped RTCP association remains bounded by the existing M6.1 implementation and is not advertised as a new B1 capability.
5. Clipping remains a deterministic metric/feature; no new root-cause-classifying clipping threshold was introduced without Golden calibration.
6. SIP/SDP semantics were not redesigned in B1. The existing M6.1 as-built behavior remains the implementation baseline and is protected by regression tests (registration, conflicting finals, SDP/call/media binding cases).

## 5. Next phase

After B1, the recommended next implementation step is **Phase C — M6.2 Reproduction Intelligence on Mock Platform**. It may implement the deterministic state machines, persistence, APIs, mock Action/Platform behavior, segmented evidence lifecycle, readiness/cleanup barriers, multi-attempt/multi-call, sufficiency and recovery logic while continuing to treat all unresolved EC-02 real DUT behavior as `CONTRACT_GAP`.
