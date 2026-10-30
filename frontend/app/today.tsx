"use client";
import {useState} from "react";
import {api,ApiError} from "./api";
import Responsibilities from "./responsibilities";
import Autonomy from "./autonomy";
import Telegram from "./telegram";
import Continuations from "./continuations";
export type PowerItem={id:string;title:string;reason:string;kind:string;score:number};
export type Urgency={score:number;bucket:"low"|"medium"|"high";factors:{deadline_hours:number|null;effort_minutes:number|null;slack_hours:number|null;importance:number;inactivity_days:number;blocked_dependencies:number;availability_minutes:number|null}};
export type TodayData={local_date:string;briefing:{content:string;local_date:string}|null;vocabulary:{word:string;definition:string;example:string;mode:"new"|"review"|"rest";reason:string;mastery_state:string;next_review_at:string|null}|null;power_list:PowerItem[];open_commitments:{id:string;title:string;deadline_hint?:string|null;urgency?:Urgency}[];active_threads?:{id:string;title:string}[];resurfaced_items:{id:string;title:string}[];evening_reflection:{status:"not_started"|"awaiting"|"completed";journal:unknown|null}};

export default function Today({data,onSessionExpired=()=>undefined,onRefresh=async()=>undefined,onOpenChat=()=>undefined}:{data:TodayData;onSessionExpired?:()=>void;onRefresh?:()=>Promise<void>;onOpenChat?:()=>void}){
  const [feedback,setFeedback]=useState("");const [busy,setBusy]=useState(false);
  async function trigger(kind:"morning_briefing"|"evening_reflection") {setBusy(true);setFeedback("");try{await api("/api/events/scheduled",{method:"POST",body:JSON.stringify({event_id:crypto.randomUUID(),kind,intended_local_date:data.local_date})});await onRefresh();setFeedback(kind==="morning_briefing"?"Briefing prepared.":"Reflection started in Chat.");if(kind==="evening_reflection")onOpenChat();}catch(cause){if(cause instanceof ApiError&&cause.status===401)onSessionExpired();setFeedback("That action could not be completed. Try again.");}finally{setBusy(false)}}
  const hasBriefing=Boolean(data.briefing);
  return <section className="view" aria-labelledby="today-heading"><header className="view-header"><div><span className="eyebrow">{data.local_date}</span><h1 id="today-heading">Today</h1><p>A calm view of what deserves your attention.</p></div>{!hasBriefing&&<button disabled={busy} onClick={()=>trigger("morning_briefing")}>Prepare briefing</button>}</header>
    <p aria-live="polite" className="status">{feedback}</p>
    <Responsibilities onExpired={onSessionExpired}/>
    <Autonomy onExpired={onSessionExpired} onRefresh={onRefresh}/>
    <Telegram onExpired={onSessionExpired}/>
    <Continuations onExpired={onSessionExpired}/>
    {data.briefing&&<article className="briefing card"><span className="badge">Briefing</span><p>{data.briefing.content}</p></article>}
    {data.power_list.length>0&&<section className="today-section"><div className="section-title"><h2>Power List</h2><span>{data.power_list.length} priorities</span></div><ol className="card-list power-list">{data.power_list.map((item,index)=><li className="card" key={item.id}><span className="rank">{index+1}</span><div><h3>{item.title}</h3><p>{item.reason}</p></div></li>)}</ol></section>}
    {data.open_commitments.length>0&&<section className="today-section"><div className="section-title"><h2>Open commitments</h2><span>Bounded to what is current</span></div><ul className="card-list">{data.open_commitments.map(item=><li className="card commitment" key={item.id}><div><h3>{item.title}</h3>{item.deadline_hint&&<p>{item.deadline_hint}</p>}</div>{item.urgency&&<div className={`urgency ${item.urgency.bucket}`}><strong>{`${item.urgency.bucket} urgency — score ${item.urgency.score}`}</strong>{item.urgency.factors.slack_hours!==null&&item.urgency.factors.effort_minutes!==null&&<span>{`${item.urgency.factors.slack_hours}h slack · ${item.urgency.factors.effort_minutes} min effort`}</span>}</div>}</li>)}</ul></section>}
    {data.vocabulary&&<section className="vocabulary card" aria-labelledby="vocabulary-heading"><div><span className="eyebrow">{data.vocabulary.mastery_state}</span><h2 id="vocabulary-heading">{data.vocabulary.mode==="new"?"Word of the Day":"Vocabulary review"}</h2></div><p className="word"><strong>{data.vocabulary.word}</strong> — {data.vocabulary.definition}</p><p><em>{data.vocabulary.example}</em></p><small>{data.vocabulary.reason}{data.vocabulary.next_review_at?` · next review ${data.vocabulary.next_review_at}`:""}</small></section>}
    {data.resurfaced_items.length>0&&<section className="today-section"><h2>Resurfaced</h2><ul className="card-list">{data.resurfaced_items.map(item=><li className="card" key={item.id}>{item.title}</li>)}</ul></section>}
    <section className="reflection card"><div><span className="eyebrow">Evening reflection</span><h2>Close the loop</h2><p>Evening reflection: {data.evening_reflection.status}</p></div>{data.evening_reflection.status!=="completed"&&(data.evening_reflection.status==="awaiting"?<button disabled={busy} onClick={onOpenChat}>Continue in Chat</button>:<button disabled={busy} onClick={()=>trigger("evening_reflection")}>Start reflection</button>)}</section>
  </section>;
}
