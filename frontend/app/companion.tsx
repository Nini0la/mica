"use client";
import {FormEvent,useEffect,useEffectEvent,useRef,useState} from "react";
import {api,ApiError} from "./api";

export type Profile={name:string;temperament:"calm"|"curious"|"playful";version:number;model_mode:string};
type Plate={id:string;title:string;description:string;kind:string;why:string;next_action:string;state:string;version:number;epistemic_status:string;parent_thread_id:string|null;ideas:{id:string;title:string;description:string}[];commitments:{id:string;title:string;state:string}[]};
type Verdict="helpful"|"unhelpful"|"acknowledged"|"completed";
type Assistance={id:string;message_id:string;intervention_type:string;execution_status:string;observed_outcome:string;context_manifest:unknown;policy:unknown;usage:unknown;tool_results:unknown;feedback:{verdict:Verdict}[];created_at:string};
type AssistancePage={items:Assistance[];growth:{dimension:string;event_type:string;assessment:string;created_at:string}[]};
type Props={thinking?:boolean;refreshKey?:string;activeThreadId:string|null;onSelect:(id:string|null)=>void;onDraft:(text:string)=>void;onSessionExpired:()=>void;onProfile?:(profile:Profile)=>void};

// Only known metadata fields may cross the diagnostics boundary, never raw context.
function metadata(value:unknown,path=""):string[]{
  if(!value||typeof value!=="object")return [];
  if(Array.isArray(value))return value.flatMap((item,index)=>metadata(item,`${path}[${index}]`));
  const allowed=new Set(["id","source_id","source_type","source_ref","thread_id","message_id","commitment_id","idea_id","version","type","kind","reason","policy_version","model","status","tool","error","trigger","notification"]);
  const numbers=new Set(["chars","approximate_tokens","input_tokens","output_tokens","total_tokens","prompt_tokens","completion_tokens","latency_ms","latency","input","output","cost","cost_usd","max_chars","max_records","max_record_chars","candidates_per_type","sources_per_record","recent_messages","query_terms","max_model_calls","max_tool_calls","max_operation_seconds"]);
  const flags=new Set(["allow_memory_write","recent_only"]);
  const containers=new Set(["items","included","excluded","after_tools","sources","source_refs","references","limits","threads","commitments","ideas","usage"]);
  return Object.entries(value).flatMap(([key,item]):string[]=>{
    const label=path?`${path}.${key}`:key;
    if((allowed.has(key)&&(typeof item==="string"||typeof item==="number"))||(numbers.has(key)&&(typeof item==="number"||item===null))||(flags.has(key)&&typeof item==="boolean"))return [`${label}: ${item===null?"not reported":String(item)}`];
    if(key==="source_refs"&&Array.isArray(item))return item.flatMap((entry,index)=>typeof entry==="string"?[`${label}[${index}]: ${entry}`]:metadata(entry,`${label}[${index}]`));
    if(containers.has(key))return metadata(item,label);
    return [];
  });
}

export default function Companion({thinking=false,refreshKey,activeThreadId,onSelect,onDraft,onSessionExpired,onProfile}:Props){
  const [opened,setOpened]=useState(false);const [profile,setProfile]=useState<Profile|null>(null);const [plates,setPlates]=useState<Plate[]>([]);const [assistance,setAssistance]=useState<AssistancePage>({items:[],growth:[]});
  const [name,setName]=useState("");const [temperament,setTemperament]=useState<Profile["temperament"]>("calm");const [busy,setBusy]=useState(false);const [error,setError]=useState("");const [notice,setNotice]=useState("");const [feedbackBusy,setFeedbackBusy]=useState<string|null>(null);
  const feedbackAttempts=useRef(new Map<string,{event_id:string;verdict:Verdict}>());
  const profileRequest=useRef(0);
  const selected=plates.find(plate=>plate.id===activeThreadId);const live=profile?.model_mode==="live";
  function failure(cause:unknown,fallback:string){if(cause instanceof ApiError&&cause.status===401){setError("Your session expired. Sign in again to continue.");onSessionExpired();}else setError(fallback)}
  function receiveProfile(next:Profile,replaceDraft=false){
    const dirty=profile&&(name!==profile.name||temperament!==profile.temperament);
    setProfile(next);if(replaceDraft||!dirty){setName(next.name);setTemperament(next.temperament)}onProfile?.(next);
  }
  const mountedProfile=useEffectEvent(receiveProfile);
  const profileFailure=useEffectEvent((cause:unknown)=>failure(cause,"Companion profile could not be loaded. Chat is still available. Retry to refresh."));
  useEffect(()=>{
    let active=true;const request=++profileRequest.current;
    void api<Profile>("/api/companion").then(result=>{if(active&&request===profileRequest.current)mountedProfile(result)}).catch(cause=>{if(active&&request===profileRequest.current)profileFailure(cause)});
    return ()=>{active=false};
  },[]);
  async function load(profileOnly=false){
    ++profileRequest.current;
    setBusy(true);setError("");setNotice("");
    try{
      if(profileOnly){receiveProfile(await api<Profile>("/api/companion"));return}
      const [nextProfile,context,records]=await Promise.all([api<Profile>("/api/companion"),api<{items:Plate[]}>("/api/context/plates"),api<AssistancePage>("/api/assistance?limit=10")]);
      receiveProfile(nextProfile);setPlates(context.items);setAssistance(records);
      if(activeThreadId&&!context.items.some(plate=>plate.id===activeThreadId))onSelect(null);
    }catch(cause){failure(cause,"Companion context could not be loaded. Chat is still available. Retry to refresh.")}
    finally{setBusy(false)}
  }
  const refresh=useEffectEvent(async()=>{
    if(!opened||!profile)return;
    try{const [context,records]=await Promise.all([api<{items:Plate[]}>("/api/context/plates"),api<AssistancePage>("/api/assistance?limit=10")]);setPlates(context.items);setAssistance(records);if(activeThreadId&&!context.items.some(plate=>plate.id===activeThreadId))onSelect(null)}
    catch(cause){failure(cause,"Context and assistance could not be refreshed. Retry to see current records.")}
  });
  // Synchronize server records after a response; state updates follow the awaited requests.
  // eslint-disable-next-line react-hooks/set-state-in-effect
  useEffect(()=>{void refresh()},[refreshKey]);
  async function save(event:FormEvent){
    event.preventDefault();if(!profile||busy||!name.trim())return;setBusy(true);setError("");setNotice("");
    try{const result=await api<Profile>("/api/companion",{method:"PATCH",body:JSON.stringify({name:name.trim(),temperament,version:profile.version})});receiveProfile(result,true);setNotice("Companion preferences saved.")}
    catch(cause){failure(cause,cause instanceof ApiError&&cause.status===409?"Preferences changed elsewhere. Refresh companion before saving again.":"Companion preferences could not be saved. Try again.")}
    finally{setBusy(false)}
  }
  async function feedback(record:Assistance,verdict:Verdict){
    if(feedbackBusy)return;setFeedbackBusy(record.id);setError("");setNotice("");
    // Uncertain retries retain the same event ID and verdict.
    const prior=feedbackAttempts.current.get(record.id);const attempt=prior?.verdict===verdict?prior:{event_id:crypto.randomUUID(),verdict};feedbackAttempts.current.set(record.id,attempt);
    try{await api(`/api/assistance/${encodeURIComponent(record.id)}/feedback`,{method:"POST",body:JSON.stringify(attempt)});setAssistance(current=>({...current,items:current.items.map(item=>item.id===record.id?{...item,feedback:[...item.feedback,{verdict}]}:item)}));feedbackAttempts.current.delete(record.id);setNotice("Feedback recorded. This is not proof of learning.")}
    catch(cause){failure(cause,"Feedback could not be confirmed. Retry the same verdict safely.")}
    finally{setFeedbackBusy(null)}
  }
  return <aside className="companion" aria-label="Chat companion">
    <div className="companion-intro">
      <svg className={`hand-creature ${thinking?"thinking":"idle"}`} viewBox="0 0 120 112" role="img" aria-label={`A small creature resting in a hand, ${thinking?"thinking":"idle"}`}>
        <path className="creature-hand" d="M18 83c12-5 19 3 29 5l25-1c8-1 18-11 27-17 6-4 12 1 7 7L85 99c-5 5-13 7-22 6l-26-4-19 2M47 88l22 3c10 1 12-9 3-11l-19-4-14-8c-6-4-12-1-11 4"/>
        <g className="creature-body"><path d="M36 68c-6-12-3-27 6-32l-2-16 15 10c8-3 17-3 24 1l14-10-2 20c7 11 7 25-3 33-13 9-43 7-52-6Z"/><path className="creature-face" d="M49 49v4m24-4v4m-18 9q7 6 13-1"/><path className="creature-arm" d="M38 62q-9 9-3 13m49-13q10 7 4 13"/></g>
        <g className="thought-dots"><circle cx="96" cy="17" r="3"/><circle cx="106" cy="9" r="2"/></g>
      </svg>
      <div className="companion-copy"><strong>{profile?.name??"Your companion"}</strong><span className="muted">{thinking?"Thinking through your message...":"A little company for unfinished ideas."}</span><small>{live?"Live AI - model tools available; memory writes need permission for each message.":profile?.model_mode==="deterministic"?"Deterministic mode - no live AI. Model tools are unavailable.":"Model mode not verified. Live AI and tools are not confirmed."}</small></div>
      <button type="button" className="secondary" disabled={busy} aria-expanded={opened} aria-controls="companion-controls" onClick={()=>{setOpened(!opened);if(!opened)void load()}}>{opened?"Close companion":"Open companion"}</button>
    </div>
    {!opened&&error&&<div className="error-row" role="alert"><span>{error}</span><button type="button" className="secondary" disabled={busy} onClick={()=>void load(true)}>Retry profile</button></div>}
    {opened&&<div id="companion-controls" className="companion-controls" aria-busy={busy}>
      {busy&&<p>Loading or saving companion...</p>}
      {error&&<p role="alert" className="error">{error}</p>}
      <button type="button" className="secondary" disabled={busy} onClick={()=>void load()}>Refresh companion</button>
      {profile&&<>
        <details><summary>Companion preferences</summary><form className="companion-settings" onSubmit={save}><label>Name<input value={name} maxLength={60} required disabled={busy} onChange={event=>setName(event.target.value)}/></label><label>Temperament<select value={temperament} disabled={busy} onChange={event=>setTemperament(event.target.value as Profile["temperament"])}>{["calm","curious","playful"].map(value=><option key={value} value={value}>{value}</option>)}</select></label><button disabled={busy||!name.trim()}>Save companion</button></form></details>
        <label>Active project or idea<select value={activeThreadId??""} disabled={thinking||busy} onChange={event=>onSelect(event.target.value||null)}><option value="">No active context</option>{plates.map(plate=><option key={plate.id} value={plate.id}>{plate.title} ({plate.kind})</option>)}</select></label>
        {plates.length===0&&<p className="muted">No context plates yet. You can still explore in Chat without saving an idea.</p>}
        {selected&&<details className="context-plate"><summary>Inspect {selected.title}</summary><span className="badge">{selected.kind}</span><p>{selected.description}</p><p><strong>Why:</strong> {selected.why||"Not recorded"}</p><p><strong>Next:</strong> {selected.next_action||"Not recorded"}</p><p className="muted">{selected.state} / {selected.epistemic_status} / version {selected.version}</p>{selected.ideas.length>0&&<><h3>Related ideas</h3><ul>{selected.ideas.map(idea=><li key={idea.id}><strong>{idea.title}</strong><p>{idea.description}</p></li>)}</ul></>}{selected.commitments.length>0&&<><h3>Commitments</h3><ul>{selected.commitments.map(item=><li key={item.id}>{item.title} ({item.state})</li>)}</ul></>}<p className="muted">Correct the title, description, kind, or why in Threads.</p></details>}
        {live&&<div><p className="muted">Creative starting points: draft only, review before sending.</p><div className="creative-actions">{["Explore alternatives","Find a small next step","Question assumptions"].map(action=><button type="button" className="secondary" key={action} disabled={thinking} onClick={()=>onDraft(`${action}${selected?` for ${selected.title}`:" for this idea"}. `)}>{action}</button>)}</div></div>}
        <details className="assistance"><summary>Assistance ({assistance.items.length})</summary><p className="muted">Feedback records your assessment, not proof of learning. Growth entries are observations, not demonstrated improvement.</p>{assistance.items.length===0&&<p>No assistance records yet.</p>}{assistance.items.map(record=><article key={record.id} className="assistance-record"><h3>{record.intervention_type}</h3><p>{record.execution_status}{record.observed_outcome?` / ${record.observed_outcome}`:""}</p><small>Message: {record.message_id}</small><div className="feedback-actions" aria-label={`Feedback for ${record.id}`}>{(["helpful","unhelpful","acknowledged","completed"] as Verdict[]).map(verdict=><button type="button" className="secondary" key={verdict} disabled={feedbackBusy!==null} aria-pressed={record.feedback.some(item=>item.verdict===verdict)} onClick={()=>void feedback(record,verdict)}>{verdict[0].toUpperCase()+verdict.slice(1)}</button>)}</div><details><summary>Diagnostics</summary><dl className="diagnostics"><dt>Assistance ID</dt><dd>{record.id}</dd><dt>Message ID</dt><dd>{record.message_id}</dd><dt>Created</dt><dd>{record.created_at}</dd></dl>
          {[["Context",record.context_manifest],["Policy",record.policy],["Usage",record.usage],["Tool receipts",record.tool_results]].map(([label,value])=><section key={String(label)} aria-label={String(label)}><h3>{String(label)}</h3>{metadata(value).map((line,index)=><p className="provenance" key={index}>{line}</p>)}</section>)}
          <p className="muted">Metadata only. Raw model context, prompts, tool arguments, and tool output text are not displayed.</p></details></article>)}{assistance.growth.length>0&&<details><summary>Reported growth observations ({assistance.growth.length})</summary>{assistance.growth.map((entry,index)=><p key={index}>{entry.dimension}: {entry.assessment} <small>{entry.event_type} / {entry.created_at}</small></p>)}</details>}</details>
      </>}
      {notice&&<p role="status">{notice}</p>}
    </div>}
  </aside>;
}
