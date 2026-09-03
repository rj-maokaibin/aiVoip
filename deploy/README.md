# VOIP AI V1.0 Production Deployment

## 1. Prepare configuration

```bash
sudo mkdir -p /etc/voip-ai/secrets
sudo cp deploy/production.env.example /etc/voip-ai/production.env
sudo chmod 600 /etc/voip-ai/production.env
sudo chmod 600 /etc/voip-ai/secrets/*
```

Replace every `<...>` placeholder. `BUILD_REVISION` is intentionally not a persistent setting: the deployment CLI
injects the checked-out immutable Git SHA into a private temporary runtime env and never rewrites
`/etc/voip-ai/production.env`. EC-02 intentionally remains a release blocker until the real DUT Platform Contract is approved.

## 2. Preflight

```bash
./deploy/voip-ai --env /etc/voip-ai/production.env preflight
```

## 3. Deploy

```bash
sudo ./deploy/voip-ai --env /etc/voip-ai/production.env prepare-host
./deploy/voip-ai --env /etc/voip-ai/production.env deploy
```

`deploy` derives `BUILD_REVISION` from the checked-out Git HEAD by default (or accepts `--revision <SHA>` only when it exactly matches HEAD), then performs preflight, a PostgreSQL backup when an existing DB is running, Docker image
build, infrastructure startup, explicit Alembic migration, application promotion, and live runtime
verification.

## 4. Verify

```bash
./deploy/voip-ai --env /etc/voip-ai/production.env verify
```

The verifier checks backend readiness, the frontend same-origin API proxy, PostgreSQL migration
head, Redis, MinIO read/write, all required Celery queues, and production configuration. It writes
exact-source evidence to `validation/production_runtime_result.json`.

## 5. Strict release

```bash
./deploy/voip-ai --env /etc/voip-ai/production.env \
  --field-pcap /data/voip-golden/8b72929e-8a06-4f1e-a922-1d3779ebbd6f.pcap release
```

The strict release path additionally runs Docker full-stack E2E, frontend Docker build evidence,
Field Golden (when supplied), and `release_readiness_gate.py --strict`.

It is fail-closed: EC-02/mock platform, missing frontend lockfile, stale source-bound evidence, or
unverified runtime conditions return non-zero instead of being reported as PASS.

## Operational commands

```bash
./deploy/voip-ai --env /etc/voip-ai/production.env status
./deploy/voip-ai --env /etc/voip-ai/production.env logs
./deploy/voip-ai --env /etc/voip-ai/production.env backup-db
./deploy/voip-ai --env /etc/voip-ai/production.env down
```

`down` never removes volumes or `/data/voip` evidence/data directories.
