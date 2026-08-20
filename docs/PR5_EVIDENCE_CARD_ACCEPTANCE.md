# PR5 Evidence Card Readability — Acceptance Contract

PR5 only changes Artifact/Report projection and explainability. It does not change deterministic Analyzer detection truth.

## Per-Finding Evidence Card

Every Finding must expose, when applicable:

1. what happened;
2. layer / Call / RTP Stream / direction / PCM Tap;
3. UTC absolute time and Call-relative T+ time;
4. normalized key measurements with units;
5. exact visual evidence linked to that Finding;
6. representative anomaly audio, or an explicit `UNAVAILABLE` reason;
7. Frame / RTP Sequence drill-down for packet anomalies;
8. preliminary interpretation;
9. root-cause boundary — what is not confirmed;
10. deterministic next action.

## Artifact binding

Event audio must not be attached by PCM Tap alone. For event clips the binding must agree on:

- event family/type;
- RTP Stream or PCM Tap/session where present;
- anomaly time window where present.

Rejected `CANDIDATE_AUDIO_CLIP` is never report-safe and never substitutes for a promoted anomaly clip.

## Renderer V2

Review PNGs are deterministic and self-describing. Plot artifacts must carry:

- title;
- source / direction;
- x/y axis semantics and units;
- anomaly window/marker when Finding-scoped;
- renderer version;
- Finding IDs;
- Call ID where available.

Required readable semantics:

- Waveform: Time (s), Amplitude (PCM), anomaly window;
- Spectrum: Frequency (Hz), Magnitude dB or energy ratio, key frequency references;
- Spectrogram: Time (s), Frequency (Hz), relative dB scale, anomaly window;
- RTP Timeline: stream direction, event type, local time window; Frame/Seq remain in Evidence Card structured drill-down;
- SIP Call Flow: production `call.ladder[]`, endpoint direction, Frame + Method/Status.

## Web / Feishu

The primary report surface shows key visual/audio artifacts inside the matching Finding. Section 10 remains the complete attachment area for leftovers and bundles.

Feishu inserts at most 12 key media objects inline per report; remaining eligible media stays in the attachment section.

## Release gates

PR5 cannot be Ready until:

- Evidence Card readability regression passes;
- Artifact binding negative-control regression passes;
- Renderer annotation regression passes;
- SIP ladder visual regression passes;
- Artifact permission regression passes;
- existing evidence-report regression and Synthetic Golden remain green;
- real Offline Golden #001 is replayed on the controlled Linux host;
- live Feishu Tenant media projection remains an explicit production environment gate.

Offline Golden #001 validates analysis/card traceability only. It does not prove Live Acquisition Reliability.
