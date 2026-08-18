import React,{useEffect,useMemo,useState} from 'react';
import {createRoot} from 'react-dom/client';
import {API,request,jsonInit} from './api';
import './evidence_report.css';

type Report={id:string;case_id:string;scope_type:string;scope_id:string;version:number;status:string;schema_version:string;composer_version:string;environment_fingerprint?:string;completeness_json?:any;boundary_json?:any;snapshot_json?:any;created_at:string;completed_at?:string};
type Finding={id:string;stable_key:string;finding_signature:string;finding_type:string;status:string;severity:string;evidence_level:string;title:string;observation:string;interpretation?:string;root_cause_boundary:string;start_time?:number;end_time?:number;representative_time?:number;scope_json?:any;metrics_json?:any;evidence_refs_json?:any[];artifact_refs_json?:any[];event_refs_json?:any[];correlation_json?:any};
type Artifact={id:string;type:string;filename:string;content_type?:string;size_bytes:number;sha256:string;metadata:any;content_url:string};
type Links={html_url?:string;json_url?:string;manifest_url?:string;bundle_url?:string;permissions:{view_report:boolean;view_raw_evidence:boolean;download_evidence_bundle:boolean;rebuild_report:boolean}};
type Retention={evidence_id:string;filename:string;kind:string;policy:string;status:string;retain_until?:string;golden_exempt:boolean;locked_by?:string;expired_at?:string;payload_available:boolean};

const severityRank:Record<string,number>={CRITICAL:4,HIGH:3,MEDIUM:2,INFO:1};
const fmt=(v?:string)=>v?new Date(v).toLocaleString():'-';
const pct=(v:any)=>`${(Number(v||0)*100).toFixed(1)}%`;
const safeJson=(v:any)=>{try{return JSON.stringify(v,null,2)}catch{return String(v)}};

function App(){
 const caseId=new URLSearchParams(location.search).get('case_id')||'';
 const [report,setReport]=useState<Report|null>(null),[findings,setFindings]=useState<Finding[]>([]),[artifacts,setArtifacts]=useState<Artifact[]>([]),[links,setLinks]=useState<Links|null>(null),[retention,setRetention]=useState<Retention[]>([]),[err,setErr]=useState(''),[busy,setBusy]=useState(false);
 async function load(){
  if(!caseId){setErr('URL 缺少 case_id');return}
  setErr('');
  try{
   const r=await request<Report>(`/cases/${caseId}/reports/evidence`);setReport(r);
   const [f,a,l,rt]=await Promise.all([
    request<Finding[]>(`/reports/evidence/${r.id}/findings`),
    request<Artifact[]>(`/reports/evidence/${r.id}/artifacts`),
    request<Links>(`/reports/evidence/${r.id}/links`),
    request<Retention[]>(`/cases/${caseId}/evidence-retention`).catch(()=>[] as Retention[]),
   ]);
   setFindings(f.sort((x,y)=>(severityRank[y.severity]||0)-(severityRank[x.severity]||0)));setArtifacts(a);setLinks(l);setRetention(rt);
  }catch(e:any){setErr(e.message||String(e))}
 }
 useEffect(()=>{load()},[caseId]);
 async function rebuild(){if(!caseId)return;try{setBusy(true);await request(`/cases/${caseId}/reports/evidence/rebuild`,jsonInit('POST',{force:true},true));await load()}catch(e:any){setErr(e.message)}finally{setBusy(false)}}
 async function bundle(profile:'INTERNAL_FULL'|'SHARE_SAFE'){if(!report)return;try{setBusy(true);const x=await request<any>(`/reports/evidence/${report.id}/bundle`,jsonInit('POST',{profile}));window.open(x.download_url,'_blank','noopener,noreferrer')}catch(e:any){setErr(e.message)}finally{setBusy(false)}}
 const p=report?.snapshot_json||{};
 const findingArtifacts=useMemo(()=>{const m:Record<string,Artifact[]>={};for(const a of artifacts){for(const id of (a.metadata?.finding_ids||[])){(m[id]||(m[id]=[])).push(a)}}return m},[artifacts]);
 if(!caseId)return <main className="er-shell"><div className="er-error">URL 缺少 case_id。</div></main>;
 return <main className="er-shell">
  <header className="er-header"><div><div className="er-eyebrow">PRELIMINARY EVIDENCE REPORT · 初步证据分析报告</div><h1>{p.case?.case_no||caseId}</h1><p>{p.case?.summary||'加载当前 Case 的 Canonical Report（权威结构化报告）'}</p></div><div className="er-actions"><button onClick={load}>刷新</button>{links?.permissions.rebuild_report&&<button disabled={busy} onClick={rebuild}>重建报告</button>}{links?.html_url&&<a className="er-btn" target="_blank" rel="noreferrer" href={links.html_url}>HTML 报告</a>}</div></header>
  {err&&<div className="er-error">{err}</div>}
  {!report?<section className="er-card"><p>尚未生成 Case 初步证据报告。Analyzer 完成后会自动生成，也可由具备权限的工程师重建。</p></section>:<>
   <section className="er-hero"><div><span className={`er-status ${report.status.toLowerCase()}`}>{report.status}</span><span>V{report.version}</span><span>{report.schema_version}</span></div><h2>{p.headline||'初步证据分析已生成'}</h2><div className="er-boundary"><b>证据边界：</b>{p.evidence_boundary?.statement||report.boundary_json?.statement||'本报告只描述证据，不确认最终 Root Cause（根因）。'}</div><div className="er-kpis"><Kpi label="Finding" value={p.finding_count??findings.length}/><Kpi label="最高等级" value={p.highest_severity||'INFO'}/><Kpi label="证据完整度" value={p.completeness?.state||report.completeness_json?.state||'-'}/><Kpi label="生成时间" value={fmt(report.completed_at||report.created_at)}/></div></section>

   <section className="er-card"><h2>1. 当前初步结论</h2><p className="er-lead">{p.preliminary_assessment?.summary||p.headline}</p><p>{p.preliminary_assessment?.recommended_next_action}</p></section>

   <section className="er-card"><h2>2. 当前重点问题</h2>{findings.length?findings.map(f=><FindingCard key={f.id} f={f} artifacts={findingArtifacts[f.id]||artifacts.filter(a=>(a.metadata?.finding_ids||[]).includes(f.id))}/>):<div className="er-ok">当前可用证据中未发现明显异常 Finding（证据问题点）。</div>}</section>

   <section className="er-card"><h2>3. 证据完整度</h2><Completeness value={p.completeness||report.completeness_json}/>{retention.some(x=>!x.payload_available)&&<div className="er-warn"><b>原始证据已过期：</b>{retention.filter(x=>!x.payload_available).map(x=>x.filename).join('、')}。报告与派生 Evidence 保留，但这些原始 Payload 已不能重新下载或重新分析。</div>}</section>

   <section className="er-card"><h2>4. 最新一次复现结果</h2><KeyValue data={p.call||p.session||{scope:report.scope_type,scope_id:report.scope_id}}/></section>

   <section className="er-card"><h2>5. 多次复现汇总</h2><MultiCall value={p.multi_call_summary}/></section>

   <section className="er-card"><h2>6. A/B 对比</h2><AB value={p.ab_comparison}/></section>

   <section className="er-card"><h2>7. 历次 Reproduction Session（复现会话）</h2><p>完整 Session/Call 历史保留在 Case 工作台；本页当前展示最新 Case Evidence Summary，并按 Environment Fingerprint（环境指纹）隔离聚合。</p><code>{report.environment_fingerprint||'-'}</code></section>

   <section className="er-card"><h2>8. 正常项 / 排除性证据</h2><ul className="er-normal">{(p.normal_and_exclusion_evidence||[]).map((x:any,i:number)=><li key={i}>✓ {x.text||safeJson(x)}</li>)}{!(p.normal_and_exclusion_evidence||[]).length&&<li>暂无可展示的排除性证据。</li>}</ul></section>

   <section className="er-card"><h2>9. 完整技术证据</h2><h3>Packet / RTP</h3><KeyValue data={p.packet_summary}/><h3>PCM / dBFS</h3><KeyValue data={p.pcm_summary}/><h3>正式 Artifact</h3><ArtifactGallery artifacts={artifacts}/></section>

   <section className="er-card"><h2>10. Evidence Bundle / 附件</h2><div className="er-actions">{links?.permissions.download_evidence_bundle?<><button disabled={busy} onClick={()=>bundle('INTERNAL_FULL')}>下载 INTERNAL_FULL</button><button disabled={busy} onClick={()=>bundle('SHARE_SAFE')}>下载 SHARE_SAFE</button></>:<span className="er-muted">当前角色没有 Evidence Bundle 下载权限。</span>}{links?.manifest_url&&<a className="er-btn secondary" target="_blank" rel="noreferrer" href={links.manifest_url}>Manifest</a>}</div><RetentionTable rows={retention}/></section>

   <section className="er-card"><h2>11. 报告版本与审计记录</h2><KeyValue data={{report_id:report.id,version:report.version,status:report.status,schema_version:report.schema_version,composer_version:report.composer_version,created_at:report.created_at,completed_at:report.completed_at}}/><p className="er-muted">报告重建、版本变化、Bundle 生成/下载入口、Retention 锁定/过期均由后端 Audit Trail（审计链）记录。</p></section>
  </>}
 </main>
}

function Kpi({label,value}:{label:string;value:any}){return <div className="er-kpi"><span>{label}</span><strong>{String(value??'-')}</strong></div>}
function KeyValue({data}:{data:any}){if(!data)return <div className="er-muted">暂无数据</div>;return <div className="er-kv">{Object.entries(data).slice(0,40).map(([k,v])=><div key={k}><span>{k}</span><code>{typeof v==='object'?safeJson(v):String(v??'-')}</code></div>)}</div>}
function Completeness({value}:{value:any}){const cap=value?.capture||{};const analyzers=value?.analyzers||{};return <div className="er-two"><div><h3>采集</h3>{Object.entries(cap).map(([k,v])=><div className="er-line" key={k}><span>{k}</span><b>{v?'✓ 可用':'⚠ 缺失/不可用'}</b></div>)}</div><div><h3>Analyzer</h3>{Object.entries(analyzers).map(([k,v]:any)=><div className="er-line" key={k}><span>{k}</span><b>{v?.status||'-'}</b></div>)}</div></div>}
function FindingCard({f,artifacts}:{f:Finding;artifacts:Artifact[]}){const frameHints=extractFrameHints(f);return <article className={`er-finding sev-${f.severity.toLowerCase()}`}><div className="er-finding-head"><div><span className={`er-sev ${f.severity.toLowerCase()}`}>{f.severity}</span><b>{f.title}</b></div><div><span>{f.evidence_level}</span><span>{f.status}</span></div></div><p><b>已观测事实：</b>{f.observation}</p>{f.interpretation&&<p><b>初步解释：</b>{f.interpretation}</p>}<p><b>时间：</b>{f.start_time??'-'} ～ {f.end_time??'-'}</p>{f.correlation_json?.first_observable_boundary?.statement&&<div className="er-boundary">{f.correlation_json.first_observable_boundary.statement}</div>}<div className="er-boundary secondary"><b>Root Cause Boundary（根因边界）：</b>{f.root_cause_boundary}</div>{frameHints.length>0&&<div className="er-frames"><b>Frame / Event 引用：</b>{frameHints.map((x,i)=><code key={i}>{x}</code>)}</div>}<details><summary>关键指标 / 研发下钻</summary><pre>{safeJson(f.metrics_json||{})}</pre><pre>{safeJson(f.event_refs_json||[])}</pre></details>{artifacts.length>0&&<ArtifactGallery artifacts={artifacts}/>}</article>}
function extractFrameHints(f:Finding){const text=safeJson({metrics:f.metrics_json,events:f.event_refs_json});const matches=text.match(/(?:frame(?:_number)?|previous_frame_number|current_frame_number|next_frame_number)[^0-9]{0,8}[0-9]+/gi)||[];return [...new Set(matches)].slice(0,20)}
function ArtifactGallery({artifacts}:{artifacts:Artifact[]}){const images=artifacts.filter(a=>a.content_type==='image/png'),audio=artifacts.filter(a=>(a.content_type||'').startsWith('audio/')||a.type==='AUDIO_CLIP'||a.filename.toLowerCase().endsWith('.wav'));const others=artifacts.filter(a=>!images.includes(a)&&!audio.includes(a));return <div className="er-artifacts">{images.map(a=><figure key={a.id}><img loading="lazy" src={`${API.replace('/api/v1','')}${a.content_url}`} alt={a.filename}/><figcaption>{a.type} · {a.filename}</figcaption></figure>)}{audio.map(a=><div className="er-audio" key={a.id}><b>{a.type}</b><span>{a.filename}</span><audio controls preload="none" src={`${API.replace('/api/v1','')}${a.content_url}`}/></div>)}{others.slice(0,30).map(a=><div className="er-file" key={a.id}><b>{a.type}</b><span>{a.filename}</span><code>SHA256 {a.sha256}</code></div>)}</div>}
function MultiCall({value}:{value:any}){const groups=value?.finding_groups||[];if(!value)return <div className="er-muted">当前没有跨 Call 聚合数据。</div>;return <><p>有效 Call 报告：{value.call_count??0}</p><table><thead><tr><th>问题</th><th>出现</th><th>复现率</th><th>稳定性</th></tr></thead><tbody>{groups.map((x:any,i:number)=><tr key={i}><td>{x.title}</td><td>{x.occurrence_calls}/{x.total_calls}</td><td>{pct(x.reproduction_rate)}</td><td>{x.stability}</td></tr>)}</tbody></table></>}
function AB({value}:{value:any}){const xs=value||[];if(!xs.length)return <div className="er-muted">当前没有满足条件的 A/B 环境对比。</div>;return <div>{xs.map((x:any,i:number)=><div className="er-ab" key={i}><b>{String(x.environment_a).slice(0,16)}… vs {String(x.environment_b).slice(0,16)}…</b><table><thead><tr><th>Finding</th><th>A</th><th>B</th><th>差异</th><th>V1 Rule</th></tr></thead><tbody>{(x.differences||[]).map((d:any,j:number)=><tr key={j}><td>{d.title}</td><td>{pct(d.environment_a_rate)}</td><td>{pct(d.environment_b_rate)}</td><td>{pct(d.absolute_rate_delta)}</td><td>{d.significant_by_v1_rule?'满足':'不满足'}</td></tr>)}</tbody></table><p className="er-muted">A/B 关联证据不独立确认因果或根因。</p></div>)}</div>}
function RetentionTable({rows}:{rows:Retention[]}){if(!rows.length)return null;return <div className="er-retention"><h3>原始 Evidence Retention（保留状态）</h3><table><thead><tr><th>文件</th><th>策略</th><th>状态</th><th>保留至</th><th>Golden</th><th>Payload</th></tr></thead><tbody>{rows.map(x=><tr key={x.evidence_id}><td>{x.filename}</td><td>{x.policy}</td><td>{x.status}</td><td>{x.retain_until?fmt(x.retain_until):'长期'}</td><td>{x.golden_exempt?'是':'否'}</td><td>{x.payload_available?'可用':'已过期'}</td></tr>)}</tbody></table></div>}

createRoot(document.getElementById('evidence-report-root')!).render(<App/>);
