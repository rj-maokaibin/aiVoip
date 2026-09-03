from pathlib import Path

path = Path("deploy/voip-ai")
text = path.read_text(encoding="utf-8")
anchor = "deploy_stack() {\n"
if anchor not in text:
    raise SystemExit("deploy_stack anchor missing")

function = r'''evidence_v2_rollout_acceptance() {
  local stage expected_revision cid observed_revision result_in_container
  local global_compose global_project global_strict
  stage="$(python3 -c 'import json; from pathlib import Path; p=json.loads(Path("deploy/evidence_v2_rollout.json").read_text(encoding="utf-8")); s=str(p.get("stage") or "").upper(); assert s in {"SHADOW","CANARY","DEFAULT"}; print(s)')"
  expected_revision="$(env_value BUILD_REVISION)"
  cid="$(compose ps -q backend)"
  [[ -n "$cid" ]] || { echo "ERROR: production backend container missing for Evidence V2 acceptance" >&2; return 1; }
  observed_revision="$(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$cid" | awk -F= '$1=="BUILD_REVISION" {print substr($0,index($0,"=")+1); exit}')"
  [[ "$observed_revision" == "$expected_revision" ]] || { echo "ERROR: Evidence V2 backend revision mismatch actual=$observed_revision expected=$expected_revision" >&2; return 1; }
  global_compose="$(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$cid" | awk -F= '$1=="PRELIMINARY_EVIDENCE_V2_COMPOSE" {print tolower($2); exit}')"
  global_project="$(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$cid" | awk -F= '$1=="PRELIMINARY_EVIDENCE_V2_PROJECT" {print tolower($2); exit}')"
  global_strict="$(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$cid" | awk -F= '$1=="PRELIMINARY_EVIDENCE_V2_STRICT_VALIDATOR" {print tolower($2); exit}')"
  [[ "$global_compose" == "true" && "$global_strict" == "true" ]] || { echo "ERROR: Evidence V2 production compose/strict defaults not active" >&2; return 1; }
  if [[ "$stage" == "DEFAULT" ]]; then
    [[ "$global_project" == "true" ]] || { echo "ERROR: Evidence V2 DEFAULT requires global V2 projection" >&2; return 1; }
  else
    [[ "$global_project" == "false" ]] || { echo "ERROR: Evidence V2 SHADOW/CANARY must keep global V1 projection" >&2; return 1; }
  fi

  mkdir -p validation
  rm -f validation/evidence_v2_production_acceptance.json
  result_in_container=/tmp/evidence-v2-production-acceptance.json
  if [[ "$stage" == "CANARY" ]]; then
    docker exec \
      -e PRELIMINARY_EVIDENCE_V2_COMPOSE=true \
      -e PRELIMINARY_EVIDENCE_V2_PROJECT=true \
      -e PRELIMINARY_EVIDENCE_V2_STRICT_VALIDATOR=true \
      "$cid" python /tools/evidence_v2_production_acceptance.py \
      --stage "$stage" --expected-revision "$expected_revision" --result "$result_in_container" || return $?
  else
    docker exec "$cid" python /tools/evidence_v2_production_acceptance.py \
      --stage "$stage" --expected-revision "$expected_revision" --result "$result_in_container" || return $?
  fi
  docker cp "$cid:$result_in_container" validation/evidence_v2_production_acceptance.json >/dev/null
  python3 -c 'import json,sys; from pathlib import Path; stage,revision=sys.argv[1:]; r=json.loads(Path("validation/evidence_v2_production_acceptance.json").read_text(encoding="utf-8")); assert r.get("status")=="PASS",r; assert r.get("stage")==stage,r; assert r.get("source_revision")==revision,r; assert r.get("v2_semantic_status")=="PASS",r; assert r.get("v2_publishable") is True,r; assert (r.get("active_projection")=="V1" and r.get("feishu_projection_attempted") is False) if stage=="SHADOW" else (r.get("active_projection")=="V2" and r.get("canonical_readback")=="PASS" and r.get("document_reused") is True),r; print(f"EVIDENCE_V2_PRODUCTION_{stage}=PASS revision={revision}")' "$stage" "$expected_revision"
}

'''

if "evidence_v2_rollout_acceptance() {" not in text:
    text = text.replace(anchor, function + anchor, 1)

runtime_line = "  perf_phase runtime_verify verify_stack\n"
if runtime_line not in text:
    raise SystemExit("runtime verify anchor missing")
accept_line = "  perf_phase evidence_v2_rollout_acceptance evidence_v2_rollout_acceptance\n"
if accept_line not in text:
    text = text.replace(runtime_line, runtime_line + accept_line, 1)

path.write_text(text, encoding="utf-8")
