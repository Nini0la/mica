"use client";
import {FormEvent, useEffect, useEffectEvent, useRef, useState} from "react";
import {api, ApiError} from "./api";
import "./autonomy.css";

type Preferences = {
  version:number; timezone:string; checkins_enabled:boolean; morning_enabled:boolean;
  evening_enabled:boolean; morning_time:string; evening_time:string;
  quiet_start:number; quiet_end:number; worker:string; last_worker_at:string|null;
};
type Commitment = {
  id:string; version:number; title:string; state:string; purpose:string; next_action:string;
  due_local:string|null; due_bound:string; next_check_at:string|null; ambiguity:string;
  followup_enabled:boolean; followup_minutes:number; followup_count:number; max_followups:number;
};
type Overview = {preferences:Preferences; commitments:Commitment[]; has_more:boolean};
type Action = {
  id:string; kind:string; state:string; reason:string; due_at:string; attempt_count:number;
  message_id:string|null; source_event_id:string|null; decision:{delivery?:string; next_check_at?:string};
};
type Revision = {version:number; source_type:string; source_id:string|null; snapshot:unknown};

export default function Autonomy({onExpired, onRefresh}:{onExpired:()=>void; onRefresh:()=>Promise<void>}) {
  const [open,setOpen]=useState(false);
  const [overview,setOverview]=useState<Overview|null>(null);
  const [actions,setActions]=useState<Action[]>([]);
  const [hasMoreActions,setHasMoreActions]=useState(false);
  const [busy,setBusy]=useState(false);
  const [error,setError]=useState("");
  const [notice,setNotice]=useState("");
  const [revisions,setRevisions]=useState<{id:string; items:Revision[]}|null>(null);
  const attempt=useRef<{fingerprint:string; event_id:string}|null>(null);
  function failure(cause:unknown) {
    if(cause instanceof ApiError&&cause.status===401)onExpired();
    setError(cause instanceof ApiError?cause.message:"Could not load or save autonomy settings.");
  }
  async function load() {
    try {
      const [current,history]=await Promise.all([
        api<Overview>("/api/autonomy?limit=100"),
        api<{items:Action[];has_more:boolean}>("/api/autonomy/actions?limit=100"),
      ]);
      setOverview(current); setActions(history.items); setHasMoreActions(history.has_more);
    } catch(cause) {failure(cause);}
  }
  const refresh=useEffectEvent(load);
  useEffect(()=>{
    if(!open)return;
    const timer=setInterval(()=>{if(document.visibilityState==="visible")void refresh();},10000);
    return ()=>clearInterval(timer);
  },[open]);
  async function savePreferences(event:FormEvent<HTMLFormElement>) {
    event.preventDefault(); if(!overview)return;
    const form=new FormData(event.currentTarget);
    setBusy(true); setError(""); setNotice("");
    try {
      await api("/api/autonomy/preferences",{method:"PATCH",body:JSON.stringify({
        version:overview.preferences.version,
        checkins_enabled:form.has("checkins_enabled"), morning_enabled:form.has("morning_enabled"),
        evening_enabled:form.has("evening_enabled"), morning_time:form.get("morning_time"),
        evening_time:form.get("evening_time"), quiet_start:Number(form.get("quiet_start")),
        quiet_end:Number(form.get("quiet_end")),
      })});
      await load(); await onRefresh(); setNotice("Autonomy preferences saved.");
    } catch(cause) {failure(cause);} finally {setBusy(false);}
  }
  async function control(item:Commitment, action:string, changes:Record<string,unknown>={}) {
    const payload={id:item.id,version:item.version,action,...changes};
    const fingerprint=JSON.stringify(payload);
    if(attempt.current?.fingerprint!==fingerprint)attempt.current={fingerprint,event_id:crypto.randomUUID()};
    setBusy(true); setError(""); setNotice("");
    try {
      await api(`/api/commitments/${item.id}/lifecycle`,{method:"POST",body:JSON.stringify({
        ...payload,event_id:attempt.current.event_id,
      })});
      attempt.current=null; setRevisions(null);
      await load(); await onRefresh(); setNotice("Commitment updated; superseded checks cancelled.");
    } catch(cause) {failure(cause);} finally {setBusy(false);}
  }
  function amend(event:FormEvent<HTMLFormElement>,item:Commitment) {
    event.preventDefault(); const form=new FormData(event.currentTarget);
    const deadline=String(form.get("deadline_text")??"").trim();
    void control(item,"update",{
      next_action:form.get("next_action"), followup_enabled:form.has("followup_enabled"),
      followup_minutes:Number(form.get("followup_minutes")), max_followups:Number(form.get("max_followups")),
      ...(deadline?{deadline_text:deadline}:{}), clear_deadline:form.has("clear_deadline"),
    });
  }
  async function inspect(item:Commitment) {
    setError("");
    try {
      const result=await api<{items:Revision[]}>(`/api/commitments/${item.id}/revisions`);
      setRevisions({id:item.id,items:result.items});
    } catch(cause) {failure(cause);}
  }
  const preferences=overview?.preferences;
  const time=(value:string|null)=>value?new Date(value).toLocaleString(undefined,{timeZone:preferences?.timezone}):"None scheduled";
  return <section className="today-section autonomy" aria-labelledby="autonomy-title">
    <div className="section-title"><h2 id="autonomy-title">Personal follow-through</h2>
      <button className="secondary" aria-expanded={open} onClick={()=>{setOpen(!open);if(!open)void load();}}>{open?"Close autonomy controls":"Open autonomy controls"}</button></div>
    <p>Owner-enabled check-ins and daily routines, saved in Chat while you’re away.</p>
    {open&&<>
      {error&&<p role="alert" className="error">{error}</p>}{notice&&<p role="status">{notice}</p>}
      <button className="text-button" disabled={busy} onClick={()=>void load()}>Refresh autonomy</button>
      {preferences&&<>
        <p>Worker: {preferences.worker.replaceAll("_"," ")} · last wake: {time(preferences.last_worker_at)}. Times shown in {preferences.timezone}.</p>
        {preferences.worker!=="recent_heartbeat"&&<p>Scheduled work needs the backend autonomy worker running. See the autonomy runbook.</p>}
        <details className="card"><summary>Check-ins, routines and quiet hours</summary>
          <form key={preferences.version} className="autonomy-form" onSubmit={savePreferences}>
            <label className="check"><input type="checkbox" name="checkins_enabled" defaultChecked={preferences.checkins_enabled}/>Enable in-app check-ins for commitments with follow-ups enabled</label>
            <label className="check"><input type="checkbox" name="morning_enabled" defaultChecked={preferences.morning_enabled}/>Enable daily morning briefing</label>
            <label>Morning time<input type="time" name="morning_time" required defaultValue={preferences.morning_time}/></label>
            <label className="check"><input type="checkbox" name="evening_enabled" defaultChecked={preferences.evening_enabled}/>Enable daily evening reflection</label>
            <label>Evening time<input type="time" name="evening_time" required defaultValue={preferences.evening_time}/></label>
            <div className="field-grid"><label>Quiet hours start<input type="number" name="quiet_start" min={0} max={23} required defaultValue={preferences.quiet_start}/></label>
              <label>Quiet hours end<input type="number" name="quiet_end" min={0} max={23} required defaultValue={preferences.quiet_end}/></label></div>
            <p>Equal quiet hours mean no quiet interval. These settings authorize in-app activity only.</p>
            <button disabled={busy}>Save autonomy preferences</button>
          </form>
        </details>
      </>}
      <ul className="card-list">{overview?.commitments.map(item=>{
        const closed=["complete","completed","cancelled","abandoned"].includes(item.state);
        return <li className="card" key={item.id}>
          <h3>{item.title}</h3><p>Status: {item.state} · version {item.version}</p>
          {item.purpose&&<p>Purpose: {item.purpose}</p>}
          <p>Next action: {item.next_action||"Not specified"}</p>
          <p>Deadline: {item.due_local?`${item.due_bound==="exclusive"?"before":"by"} ${time(item.due_local)}`:"Not specified"}</p>
          <p>Next check: {time(item.next_check_at)} · follow-ups {item.followup_count}/{item.max_followups}</p>
          {item.ambiguity!=="none"&&<p>Deadline needs clarification.</p>}
          <div className="actions">
            {closed?<button disabled={busy} onClick={()=>void control(item,"reopen")}>Reopen</button>:<>
              <button disabled={busy} onClick={()=>void control(item,item.state==="paused"?"resume":"pause")}>{item.state==="paused"?"Resume":"Pause"}</button>
              <button disabled={busy} onClick={()=>void control(item,"complete")}>Mark complete</button>
              <button className="secondary" disabled={busy} onClick={()=>void control(item,"cancel")}>Cancel commitment</button>
            </>}
            <button className="secondary" disabled={busy} onClick={()=>void inspect(item)}>Show corrections</button>
          </div>
          {!closed&&<details><summary>Correct plan and follow-ups</summary>
            <form key={item.version} className="autonomy-form" onSubmit={event=>amend(event,item)}>
              <label>Next action<input name="next_action" maxLength={500} defaultValue={item.next_action}/></label>
              <label>New deadline (in {preferences?.timezone})<input name="deadline_text" maxLength={500} placeholder="Thursday at 6pm"/></label>
              <label className="check"><input type="checkbox" name="clear_deadline"/>Clear deadline</label>
              <label className="check"><input type="checkbox" name="followup_enabled" defaultChecked={item.followup_enabled}/>Enable follow-ups for this commitment</label>
              <label>Follow-up interval (minutes)<input type="number" name="followup_minutes" min={1} max={10080} required defaultValue={item.followup_minutes}/></label>
              <label>Maximum follow-ups for this plan<input type="number" name="max_followups" min={0} max={10} required defaultValue={item.max_followups}/></label>
              <button disabled={busy}>Save plan</button>
            </form>
          </details>}
          {revisions?.id===item.id&&<details open><summary>Correction evidence</summary>{revisions.items.map(row=><div key={row.version}><p>Version {row.version} · {row.source_type} · {row.source_id}</p><pre>{JSON.stringify(row.snapshot,null,2)}</pre></div>)}</details>}
        </li>;
      })}</ul>
      {overview?.has_more&&<p>Showing the 100 most recently updated commitments.</p>}
      <details className="card"><summary>Execution history ({actions.length})</summary>
        {actions.length===0&&<p>No autonomous wake-ups recorded yet.</p>}
        <ol className="card-list">{actions.map(action=><li key={action.id}>
          <p><strong>{action.kind.replaceAll("_"," ")}</strong> · {action.state} · due {time(action.due_at)}</p>
          <p>Reason: {action.reason.replaceAll("_"," ")} · attempts: {action.attempt_count} · delivery: {action.decision.delivery??"none"}</p>
          <p>Action: <code>{action.id}</code>{action.source_event_id&&<> · event: <code>{action.source_event_id}</code></>}{action.message_id&&<> · saved Chat message: <code>{action.message_id}</code></>}</p>
        </li>)}</ol>
        {hasMoreActions&&<p>Showing the 100 most recent wake-ups.</p>}
      </details>
    </>}
  </section>;
}
