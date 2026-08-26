# Production SSH root cutover acceptance

Pass requires all of the following in one guarded run:

- current production runtime is Poseidon + Capture V2 real and still has `SSH_USERNAME=admin` before mutation;
- no nonterminal ReproductionSession and no ACTIVE/QUARANTINED diagnostic lock;
- the protected `/etc/voip-ai/production.env` is backed up before mutation;
- exactly one `SSH_USERNAME=admin` entry is atomically changed to `SSH_USERNAME=root`;
- only Python application services are force-recreated;
- every recreated application container exposes `SSH_USERNAME=root` and `CREDENTIAL_PROVIDER=poseidon`;
- backend runtime remains Capture V2 real;
- production reproduction-worker resolves APF3260-M credential by SN/IP only and successfully connects through SSH as `root`;
- `br-lan_400` is present;
- any failed post-check restores the protected env and recreates application services back to the pre-change state.
