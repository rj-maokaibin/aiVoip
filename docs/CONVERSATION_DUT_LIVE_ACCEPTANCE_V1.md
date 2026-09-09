# Conversation → DUT Live Acceptance V1

## 1. Purpose

This gate closes the real-environment gap left intentionally open by Conversation Feishu Live Acceptance V1.
It validates one dedicated acceptance incident through the normal Production product path:

```text
real Feishu user message
→ FeishuCaseBinding / Case
→ Conversation context
→ real ReproductionSession
→ ARMED / WATCHING
→ real phone call
→ PCAP + PCM RX + PCM TX + Debug
→ deterministic Packet / Media analysis
→ deterministic Diagnosis
→ Cleanup Verified
→ diagnosis report
→ real Feishu completion reply
```

It is an acceptance observer, not an alternate execution engine.

## 2. Non-negotiable safety boundary

The gate itself is read-only. It MUST NOT:

- create a synthetic Feishu event or `ConversationTurn`;
- create a Case or ReproductionSession;
- synthesize OFFHOOK / ONHOOK / SIP / RTP / call events;
- run SSH/AIM/tcpdump/PCM commands;
- mutate the DUT, PBX, gateway or firewall;
- start/cancel reproduction directly;
- send a synthetic acceptance reply;
- promote a Case to Golden or change AI promotion stage.

All device control and user-visible replies must come from the normal Production product flow.

## 3. Dedicated acceptance tag and automatic Case discovery

The initial real Feishu incident MUST contain a unique tag matching:

```text
CONV-DUT-E2E-[A-Z0-9_-]{8,64}
```

Example:

```text
CONV-DUT-E2E-20260829A1
```

The live observer no longer requires a human to copy the generated Case number. `tools/conversation_dut_case_resolver.py` runs read-only inside the Production runtime and waits for exactly one ACTIVE `FeishuCaseBinding` whose authoritative `source_normalized_text` contains the tag and whose source message has the real `om_...` shape.

Resolution is fail-closed:

- zero matches → `WAITING_FEISHU_CASE` until the bounded resolution timeout;
- exactly one Case → continue with that Case;
- more than one Case for the tag → `ACCEPTANCE_TAG_NOT_UNIQUE` and BLOCKED;
- there is no fallback to the newest/recent Case.

Raw Feishu message IDs, sender IDs, tenant keys and chat IDs are never written to the uploaded acceptance artifact; only SHA-256 values are retained.

## 4. Evidence contract

### 4.1 Feishu ingress

Required:

- active `FeishuCaseBinding` for the uniquely resolved Case;
- real source message ID with `om_...` shape;
- exact dedicated acceptance tag in source normalized text;
- active FEISHU Conversation for the same tenant/chat and active Case;
- at least one real USER `ConversationTurn` in that Conversation.

The current product flow mirrors the initial `NEW_DIAGNOSIS` source message into `ConversationTurn` at Case binding time. The observer records `initial_source_turn_persisted` and never fabricates a missing row.

### 4.2 Single real-DUT flow

The gate reuses `tools/m7_acceptance_strict_audit.py` and therefore requires the latest ReproductionSession itself to be a strict real-DUT session. Cross-session mosaicking is not accepted.

Required M7 product-flow evidence:

- Case and DUT binding;
- real Voice context;
- PCAP;
- PCM RX;
- PCM TX;
- Debug/log evidence;
- deterministic Packet/SIP/RTP analyzer provenance;
- deterministic PCM/Media analyzer provenance;
- deterministic Diagnosis baseline;
- reproduction ARMED;
- real Call detected;
- Cleanup Verified;
- no residual active diagnostic lock;
- generated diagnosis report.

AI SHADOW/grounding and Golden materialization are independent maturity gates and are not prerequisites for this Conversation product-flow acceptance.

### 4.3 Real call requirement

`FXS_MONITOR_READY` must be present in the same ReproductionSession.
At least one `ReproductionCall.status=ANALYZED` must be produced by the real product flow.

The acceptance workflow never simulates a call. A person or an independently controlled real phone must make one real call after the product reaches WATCHING.

### 4.4 Diagnosis and reply

Required:

- post-session `DiagnosisRun.status=DIAGNOSED` with non-empty deterministic `decision_json`;
- matching `FEISHU_CASE_FEEDBACK` completion idempotency record for that DiagnosisRun;
- `FeishuReplyDeliveryTrace.stage=SENT` to the original Case source message after diagnosis completion;
- at least one delivery attempt.

This proves the Production diagnosis-to-user delivery chain, rather than sending an acceptance-only synthetic reply.

## 5. Live trigger

After this workflow is merged to master, only the repository owner may start the observer through an exact-master PR comment:

```text
/run-conversation-dut-e2e <exact-master-sha> <acceptance-tag>
```

Example shape:

```text
/run-conversation-dut-e2e 0123456789abcdef0123456789abcdef01234567 CONV-DUT-E2E-20260829A1
```

The observer may be started before the real Feishu incident is sent. It waits up to 900 seconds for a unique matching Case, then observes the resolved Case for the normal Production flow.

The command or resolver is rejected/blocked if:

- actor is not repository owner;
- master moved;
- SHA is malformed;
- acceptance tag is malformed;
- the tag resolves to multiple Cases;
- the matched source is not a real Feishu `om_...` message.

## 6. Lifecycle phases

Case discovery adds:

- `WAITING_FEISHU_CASE`
- `CASE_RESOLVED`

The main observer then reports one of:

- `WAITING_CONVERSATION_TURN`
- `WAITING_REPRODUCTION`
- `WAITING_ARM`
- `WAITING_REAL_CALL`
- `WAITING_CLEANUP`
- `WAITING_ANALYSIS`
- `WAITING_REPLY`
- `PASS`
- `BLOCKED`

A terminal reproduction without an analyzed real call is `BLOCKED`, not a synthetic success.

## 7. Human intervention

Only two environment actions are inherently human/external and cannot be replaced by the acceptance code:

1. send one dedicated real Feishu incident containing the acceptance tag and real DUT information/symptom;
2. after the normal product flow reaches WATCHING, perform one real phone call.

The user does not need to copy the generated Case number into GitHub. Case discovery, exact-master preflight, evidence observation, analysis, cleanup/reply verification and artifact upload are automated and read-only from the acceptance gate's perspective.

## 8. Relation to other gates

- **Conversation P0/P1 Gate**: semantic/state software correctness.
- **Conversation Feishu Live Acceptance V1**: explicit one-message real Feishu transport acceptance.
- **Real SIP Registration A-B-A**: causal real-DUT SIP registration fault/recovery gate.
- **M7 Strict Audit**: single real-DUT evidence/provenance closure.
- **Conversation → DUT Live Acceptance V1**: real user Conversation correlated to that real-DUT closed loop and the final real reply.

No one of these gates should be reported as a substitute for another.
