# Production SSH username contract

Production DUT SSH uses `SSH_USERNAME=root`.

The protected deployment environment file `/etc/voip-ai/production.env` is the runtime authority. The production cutover workflow changes only the single `SSH_USERNAME` entry from `admin` to `root`, backs up the original file with mode 600, recreates only Python application services, and verifies the normal reproduction-worker credential path against APF3260-M using Poseidon resolution by SN/IP only. Any post-change verification failure triggers automatic restoration of the protected environment file and application container recreation.

`deploy/production.env.example` is kept consistent with this runtime contract so new production deployments do not regress to `admin`.
