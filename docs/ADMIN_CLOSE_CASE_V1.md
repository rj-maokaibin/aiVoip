# Administrative Case Close V1

This controlled path retires a stale Case without asserting that a fix was verified.

## Semantics

- `CASE_ADMIN_CLOSED` moves an eligible non-terminal Case directly to `CLOSED`.
- It never emits `FIX_VERIFIED` and never materializes `RESOLVED`.
- `FAILED` remains a distinct terminal outcome and cannot be rewritten as `CLOSED`.
- `CLOSED` is idempotent.

## Safety

Before mutation, the Production tool requires:

1. exact `VOIP-YYYYMMDD-XXXXXX` Case number;
2. controlled `github-admin:` actor;
3. non-empty fixed reason;
4. zero non-terminal `ReproductionSession` rows;
5. Feishu binding lifecycle schema availability when a chat binding exists.

After mutation it verifies:

- Case status is `CLOSED`;
- `CASE_ADMIN_CLOSED` history exists (unless the Case was already closed);
- no active reproduction exists;
- the retired Case is no longer the Active Case for its Feishu tenant/chat.

Raw Feishu tenant/chat identifiers are not written to uploaded acceptance evidence; hashes are used instead.

## Live command

After merge, repository owner only:

```text
/admin-close-case <exact-master-sha> <case-no>
```

The workflow resolves the Production DB from the controlled runtime, runs a non-mutating preflight, then re-checks the same guards during apply. If an active reproduction exists the close is blocked and normal reproduction cleanup must complete first.
