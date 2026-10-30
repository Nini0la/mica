"use client";
import {useEffect,useEffectEvent,useRef,useState} from "react";
import {api,ApiError} from "./api";

type Plan={id:string;version:number;topic:string;mode:string;state:string;next_due_at:string|null;timezone:string;interval_seconds:number|null;occurrences:number;max_occurrences:number};
type Data={reminders:Plan[];proposals:{id:string;version:number;title:string;state:string;arguments:unknown}[];workflows:unknown[];scheduling_policy:unknown};
export default function Continuations({onExpired}:{onExpired:()=>void}){
  const [open,setOpen]=useState(false);const [data,setData]=useState<Data|null>(null);
  const [error,setError]=useState("");const [busy,setBusy]=useState(false);
  const attempt=useRef<{key:string;event_id:string}|null>(null);
  function failed(cause:unknown){if(cause instanceof ApiError&&cause.status===401)onExpired();setError(cause instanceof Error?cause.message:"Could not load actions.");}
  async function load(){try{setData(await api<Data>("/api/continuations"));}catch(cause){failed(cause);}}
  const refresh=useEffectEvent(load);
  useEffect(()=>{if(!open)return;const timer=setInterval(()=>{if(document.visibilityState==="visible")void refresh();},10000);return()=>clearInterval(timer);},[open]);
  async function control(plan:Plan,action:string){
    const key=JSON.stringify({id:plan.id,version:plan.version,action});
    if(attempt.current?.key!==key)attempt.current={key,event_id:crypto.randomUUID()};
    setBusy(true);setError("");
    try{await api(`/api/reminder-plans/${plan.id}/control`,{method:"POST",body:JSON.stringify({...JSON.parse(key),event_id:attempt.current.event_id})});attempt.current=null;await load();}
    catch(cause){failed(cause);}finally{setBusy(false);}
  }
  return <section className="today-section" aria-labelledby="continuation-heading">
    <div className="section-title"><h2 id="continuation-heading">Reminders and proposals</h2><button aria-expanded={open} className="secondary" onClick={()=>{setOpen(!open);if(!open)void load();}}>{open?"Close reminders":"Open reminders"}</button></div>
    {open&&<>{error&&<p role="alert">{error}</p>}{data&&<>
      <p>One-time reminders send once. Recurring check-ins have a separate interval and occurrence limit. Pending proposals wait for your acceptance.</p>
      <ul className="card-list">{data.reminders.map(plan=><li className="card" key={plan.id}><h3>{plan.topic}</h3><p>{plan.mode==="once"?"One-time reminder":"Recurring check-in"} · {plan.state}</p><p>Next: {plan.next_due_at?new Date(plan.next_due_at).toLocaleString(undefined,{timeZone:plan.timezone}):"None scheduled"} ({plan.timezone})</p>{plan.interval_seconds&&<p>Every {plan.interval_seconds} seconds · {plan.occurrences}/{plan.max_occurrences}</p>}{["active","paused"].includes(plan.state)&&<div className="actions"><button disabled={busy} onClick={()=>void control(plan,plan.state==="paused"?"resume":"pause")}>{plan.state==="paused"?"Resume reminder":"Pause reminder"}</button><button disabled={busy} onClick={()=>void control(plan,"cancel")}>Cancel reminder</button></div>}</li>)}</ul>
      <h3>Conversation proposals</h3>{data.proposals.length===0?<p>No proposals recorded.</p>:<ul className="card-list">{data.proposals.map(p=><li className="card" key={p.id}><strong>{p.title}</strong><p>{p.state} · version {p.version}</p><details><summary>Stored parameters</summary><pre className="context-json">{JSON.stringify(p.arguments,null,2)}</pre></details></li>)}</ul>}
      <p>Accept, clarify, pause or cancel a proposal naturally in Chat or Telegram. You do not need to repeat its stored parameters.</p>
      <details><summary>Workflow state and scheduling policy</summary><pre className="context-json">{JSON.stringify({workflows:data.workflows,policy:data.scheduling_policy},null,2)}</pre></details>
    </>}</>}
  </section>;
}
