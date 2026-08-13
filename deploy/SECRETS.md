# Production Secrets Contract

Production secrets must never be committed to the repository or written to application logs.
The deployment runner expects the production env file to be mode `0600` (or stricter) and all
Docker secret source files to have no group/world permissions.

Recommended host layout:

```text
/etc/voip-ai/
├── production.env          # chmod 600
└── secrets/
    ├── auth_gateway_hmac           # chmod 600
    ├── minio_access_key            # chmod 600
    ├── minio_secret_key            # chmod 600
    ├── credential_api_token        # chmod 600
    ├── feishu_app_secret           # chmod 600
    └── feishu_verification_token   # chmod 600
```

The container sees these values only through `/run/secrets/*`:

```text
AUTH_GATEWAY_HMAC_SECRET_FILE=/run/secrets/auth_gateway_hmac
MINIO_ACCESS_KEY_FILE=/run/secrets/minio_access_key
MINIO_SECRET_KEY_FILE=/run/secrets/minio_secret_key
CREDENTIAL_API_TOKEN_FILE=/run/secrets/credential_api_token
FEISHU_APP_SECRET_FILE=/run/secrets/feishu_app_secret
FEISHU_VERIFICATION_TOKEN_FILE=/run/secrets/feishu_verification_token
```

Docker Compose resolves the host-side paths from the `*_HOST_FILE` variables in
`deploy/production.env.example`.

`POSTGRES_PASSWORD`, `MINIO_ROOT_USER`, `MINIO_ROOT_PASSWORD`, and the resolved
`DATABASE_URL` remain bootstrap values in the protected production env file because the current
upstream service bootstrap contract consumes environment values. They must be strong, non-default,
and the env file must remain untracked and mode 0600.

The deployment preflight rejects missing/empty secret files, group/world-readable secret files,
known default credentials, unresolved `<...>` placeholders, anonymous production auth, wildcard
CORS, and mock credential providers.
