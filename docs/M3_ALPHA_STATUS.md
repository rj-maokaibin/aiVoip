# M3 PCM Intelligence — Alpha Status

Implemented:

- Profile-driven private PCM tap mapping.
- Raw PCAP UDP extraction for private PCM streams.
- 8 kHz signed 16-bit little-endian mono profile calibrated on real capture.
- Session segmentation.
- Packet interval/gap evidence.
- RMS, dBFS, peak, DC offset, clipping.
- 50/60 Hz harmonic-family evidence score.
- High-confidence Goertzel DTMF candidate detection and sequence grouping.
- Cross-layer DTMF/SIP correlation utility.
- Separate Celery `pcm` queue/worker and `/analyze/pcm` API.
- G.711 A-law/u-law decoder foundation for RTP audio.

Not yet production-complete:

- PCAPNG private PCM extraction (will use TShark field extraction rather than reimplement PCAPNG).
- WAV artifact persistence in MinIO/Artifact table.
- Spectrogram / waveform artifacts.
- Click/pop, adaptive silence, comb/narrow-band noise, echo ERL/ERLE.
- RTP payload -> WAV worker integration.
- Automatic RTP ↔ PCM waveform correlation in the platform job graph.
- Golden Sample corpus expansion and threshold tuning.
