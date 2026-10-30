"use client";
import {FormEvent,useEffect,useEffectEvent,useRef,useState} from "react";
import Image from "next/image";
import {api,ApiError} from "./api";
import Modal from "./modal";
import "./responsibilities.css";

type Account={id:string;address:string;assistant_name:string;on_behalf_of:string;enabled:boolean;quiet_start:number;quiet_end:number};
type Connection={account:Account|null;connection:{status:string;qr_image?:string;last_error?:string};worker?:{status:string;last_seen_at:string|null}};
type Delivery={id:string;responsibility_id:string|null;direction:string;content:string;state:string;purpose:string;created_at:string;interpretation:unknown;last_error:string|null};
type Responsibility={id:string;scope_hash:string;title:string;due_at:string;state:string;recipient_name:string;recipient_address:string;expected_evidence:string;followup_minutes:number;max_followups:number;followup_count:number;attention_reason:string|null;version:number;initial_message:string;followup_message:string;last_report:string;reported_complete_at:string|null;verified_at:string|null;verification_note:string;agreement_at:string|null;messages?:Delivery[];events?:{id:string;kind:string;created_at:string;details:unknown}[]};
type ScheduledResponsibility=Responsibility&{next_action_at?:string|null};

export default function Responsibilities({onExpired}:{onExpired:()=>void}){
  const [open,setOpen]=useState(false);const [connection,setConnection]=useState<Connection|null>(null);
  const [items,setItems]=useState<ScheduledResponsibility[]>([]);const [selected,setSelected]=useState<Responsibility|null>(null);
  const [error,setError]=useState("");const [busy,setBusy]=useState(false);const [approved,setApproved]=useState(false);
  const [note,setNote]=useState("");const [notice,setNotice]=useState("");
  const draftAttempt=useRef<{fingerprint:string;event_id:string}|null>(null);
  function failure(cause:unknown){if(cause instanceof ApiError&&cause.status===401)onExpired();setError(cause instanceof ApiError?cause.message:"Operation failed; refresh and retry.")}
  async function load(){
    try{const [channel,list]=await Promise.all([api<Connection>("/api/operations/channel"),api<{items:Responsibility[]}>("/api/responsibilities")]);setConnection(channel);setItems(list.items)}catch(cause){failure(cause)}
  }
  const refresh=useEffectEvent(load);
  useEffect(()=>{if(!open)return;const timer=setInterval(()=>{if(document.visibilityState==="visible")void refresh()},5000);return()=>clearInterval(timer)},[open]);
  async function setup(event:FormEvent<HTMLFormElement>){
    event.preventDefault();const form=new FormData(event.currentTarget);setBusy(true);setError("");
    try{await api("/api/operations/channel",{method:"POST",body:JSON.stringify({address:form.get("address"),on_behalf_of:form.get("on_behalf_of"),quiet_start:Number(form.get("quiet_start")),quiet_end:Number(form.get("quiet_end"))})});await load()}catch(cause){failure(cause)}finally{setBusy(false)}
  }
  async function channelAction(action:string){setBusy(true);setError("");try{await api(`/api/operations/channel/${action}`,{method:"POST",body:"{}"});await load()}catch(cause){failure(cause)}finally{setBusy(false)}}
  async function draft(event:FormEvent<HTMLFormElement>){
    event.preventDefault();const form=new FormData(event.currentTarget);setBusy(true);setError("");
    const due=new Date(String(form.get("due_at")));if(Number.isNaN(due.getTime())){setBusy(false);setError("Choose a valid deadline.");return}
    const payload={title:form.get("title"),recipient_name:form.get("recipient_name"),recipient_address:form.get("recipient_address"),due_at:due.toISOString(),expected_evidence:form.get("expected_evidence"),followup_minutes:Number(form.get("followup_minutes")),max_followups:Number(form.get("max_followups"))};
    const fingerprint=JSON.stringify(payload);
    if(draftAttempt.current?.fingerprint!==fingerprint)draftAttempt.current={fingerprint,event_id:crypto.randomUUID()};
    try{const result=await api<Responsibility>("/api/responsibilities",{method:"POST",body:JSON.stringify({...payload,event_id:draftAttempt.current.event_id})});draftAttempt.current=null;setSelected(result);setApproved(false);setNotice("Draft saved. No contact has been authorized.");await load()}catch(cause){failure(cause)}finally{setBusy(false)}
  }
  async function inspect(id:string){setBusy(true);setError("");setApproved(false);setNote("");try{setSelected(await api<Responsibility>(`/api/responsibilities/${id}`))}catch(cause){failure(cause)}finally{setBusy(false)}}
  async function control(action:string){if(!selected)return;setBusy(true);setError("");try{const updated=await api<Responsibility>(`/api/responsibilities/${selected.id}/control`,{method:"POST",body:JSON.stringify({action,version:selected.version,scope_hash:selected.scope_hash,note,approve_external_contact:action==="authorize"&&approved})});setSelected(updated);setApproved(false);setNotice(action==="authorize"?"Approved scope saved. The worker will contact the recipient when the channel is available and quiet hours permit.":"Responsibility updated.");await load()}catch(cause){failure(cause)}finally{setBusy(false)}}
  async function resolve(message:Delivery,decision:string){if(!selected)return;setBusy(true);try{await api(`/api/responsibilities/${selected.id}/deliveries/${message.id}`,{method:"POST",body:JSON.stringify({decision,version:selected.version})});await inspect(selected.id);await load()}catch(cause){failure(cause)}finally{setBusy(false)}}
  const attention=items.filter(item=>item.attention_reason);
  return <section className="today-section operations" aria-labelledby="responsibilities-title">
    <div className="section-title"><h2 id="responsibilities-title">Delegated responsibilities</h2><button className="secondary" aria-expanded={open} onClick={()=>{setOpen(!open);if(!open)void load()}}>{open?"Close responsibilities":"Open responsibilities"}</button></div>
    <p>Delegate a result, authorize a bounded contact plan, and review evidence before calling it verified.</p>
    {open&&<>
      {error&&!selected&&<p role="alert" className="error">{error}</p>}{notice&&<p role="status">{notice}</p>}
      <button className="text-button" onClick={()=>void load()}>Refresh responsibilities</button>
      {connection&&!connection.account&&<form className="card operations-form" onSubmit={setup}>
        <h3>Dedicated assistant WhatsApp account</h3><p>Use a separate assistant number. The assistant identifies itself and says whom it represents in every message.</p>
        <label>Assistant number (international format)<input name="address" type="tel" placeholder="+441234567890" pattern="\+[1-9][0-9]{7,14}" required/></label>
        <label>Name the assistant represents<input name="on_behalf_of" maxLength={100} required/></label>
        <div className="field-grid"><label>Quiet hours start (your timezone)<input name="quiet_start" type="number" min={0} max={23} defaultValue={21}/></label><label>Quiet hours end<input name="quiet_end" type="number" min={0} max={23} defaultValue={8}/></label></div>
        <button disabled={busy}>Save assistant channel</button>
      </form>}
      {connection?.account&&<>
        <details className="card"><summary>WhatsApp connection · {connection.connection.status}</summary>
          <p>{connection.account.assistant_name}, AI assistant for {connection.account.on_behalf_of} · {connection.account.address}</p>
          <p>Quiet hours: {connection.account.quiet_start}:00–{connection.account.quiet_end}:00 in your configured timezone. Equal hours mean no quiet interval.</p>
          <p>Channel account ID: <code>{connection.account.id}</code></p>
          <p>Follow-through worker: {(connection.worker?.status??"not_started").replaceAll("_"," ")}. A paired phone alone does not run scheduled follow-ups.</p>
          {connection.connection.status==="not_configured"&&<p>Configure CHANNEL_ACCOUNT_ID, CHANNEL_BRIDGE_TOKEN and WHATSAPP_ASSISTANT_NUMBER, then run the bridge and responsibility worker. See docs/OPERATIONS.md.</p>}
          {connection.connection.last_error&&<p role="status">Connection issue: {connection.connection.last_error}</p>}
          {connection.connection.qr_image?.startsWith("data:image/png;base64,")&&<div><p>On the dedicated assistant phone: WhatsApp → Linked devices → Link a device.</p><Image unoptimized src={connection.connection.qr_image} width={256} height={256} alt="Pair the dedicated assistant WhatsApp account"/></div>}
          <div className="actions"><button className="secondary" disabled={busy||!connection.account.enabled} onClick={()=>void channelAction("connect")}>Pair / reconnect</button>{connection.account.enabled?<button className="secondary" disabled={busy} onClick={()=>void channelAction("disable")}>Disable contact</button>:<button className="secondary" disabled={busy} onClick={()=>void channelAction("enable")}>Enable channel (plans stay paused)</button>}<button className="secondary" disabled={busy} onClick={()=>void channelAction("logout")}>Log out WhatsApp</button></div>
        </details>
        <details className="card"><summary>Delegate a new responsibility</summary><form className="operations-form" onSubmit={draft}>
          <label>Expected outcome<input name="title" maxLength={500} required/></label>
          <label>Person responsible<input name="recipient_name" maxLength={100} required/></label>
          <label>Recipient number<input name="recipient_address" type="tel" pattern="\+[1-9][0-9]{7,14}" placeholder="+441234567891" required/></label>
          <label>Deadline (this device’s local time)<input name="due_at" type="datetime-local" required/></label>
          <label>Evidence or confirmation to request<textarea name="expected_evidence" maxLength={1000} required/></label>
          <div className="field-grid"><label>Follow-up interval in minutes<input name="followup_minutes" type="number" min={60} max={10080} defaultValue={1440} required/></label><label>Maximum follow-ups<input name="max_followups" type="number" min={0} max={5} defaultValue={2} required/></label></div>
          <p>Saving creates a draft only. You review the recipient and messages before authorizing contact.</p><button disabled={busy}>Save responsibility draft</button>
        </form></details>
      </>}
      <h3>Needs your attention ({attention.length})</h3>{attention.length===0&&<p>No responsibility currently needs intervention.</p>}
      <ul className="card-list">{[...attention,...items.filter(item=>!item.attention_reason)].map(item=><li className="card" key={item.id}>
        <div className="card-top"><span className="badge">{item.state.replaceAll("_"," ")}</span>{item.attention_reason&&<strong>{item.attention_reason.replaceAll("_"," ")}</strong>}</div>
        <h3>{item.title}</h3><p>{item.recipient_name} · due {new Date(item.due_at).toLocaleString()}</p>
        <p>Follow-ups: {item.followup_count}/{item.max_followups}. {item.verified_at?"Completion verified by you.":item.state==="completion_reported"?"Completion reported, not verified.":"No verified completion yet."}</p>
        {item.next_action_at&&<p>Next scheduled check: {new Date(item.next_action_at).toLocaleString()}</p>}
        <button className="secondary" disabled={busy} onClick={()=>void inspect(item.id)}>Review responsibility</button>
      </li>)}</ul>
    </>}
    {selected&&<Modal labelledBy="responsibility-review" className="operations-modal" onClose={()=>setSelected(null)}>
      <div className="section-title"><h2 id="responsibility-review">Review responsibility</h2><button className="secondary" onClick={()=>setSelected(null)}>Close</button></div>
      <h3>{selected.title}</h3><p><strong>{selected.recipient_name}</strong> · {selected.recipient_address}</p><p>Deadline: {new Date(selected.due_at).toLocaleString()} · {selected.state.replaceAll("_"," ")}</p>
      {error&&<p role="alert" className="error">{error}</p>}
      <details open><summary>Initial message</summary><pre>{selected.initial_message}</pre></details>
      <details><summary>Follow-up message (at most {selected.max_followups}, every {selected.followup_minutes} minutes)</summary><pre>{selected.followup_message}</pre></details>
      {["draft","paused","escalated"].includes(selected.state)&&<div className="card"><label className="check"><input type="checkbox" checked={approved} onChange={e=>setApproved(e.target.checked)}/>I authorize this recipient, the displayed messages and the stated follow-up limits on my behalf.</label><button disabled={busy||!approved} onClick={()=>void control("authorize")}>Authorize contact</button></div>}
      {selected.last_report&&<div><h3>Latest recipient report — not independent verification</h3><pre>{selected.last_report}</pre></div>}
      {selected.reported_complete_at&&["completion_reported","paused","escalated"].includes(selected.state)&&<div className="operations-form"><label>Evidence you reviewed<textarea value={note} onChange={e=>setNote(e.target.value)} maxLength={2000}/></label><button disabled={busy||!note.trim()} onClick={()=>void control("verify")}>Confirm verified completion</button></div>}
      {selected.verified_at&&<p>Verified by you: {selected.verification_note}</p>}
      <div className="actions">{!["cancelled","verified"].includes(selected.state)&&<><button className="secondary" disabled={busy} onClick={()=>void control("pause")}>Pause</button><button className="secondary" disabled={busy} onClick={()=>void control("cancel")}>Cancel responsibility</button></>}{selected.attention_reason&&<button className="secondary" disabled={busy} onClick={()=>void control("acknowledge")}>Acknowledge update</button>}<button className="secondary" disabled={busy} onClick={()=>void inspect(selected.id)}>Refresh details</button></div>
      <h3>Communication timeline</h3>{selected.messages?.map(message=><article className="card" key={message.id}><strong>{message.direction} · {message.state}</strong>{!message.responsibility_id&&<p>Unmatched reply from this contact — not attributed to this task.</p>}<pre>{message.content}</pre><small>{new Date(message.created_at).toLocaleString()}</small>{["uncertain","failed"].includes(message.state)&&<div><p>Delivery is unresolved. Confirm independently before marking sent; cancellation does not undo a message already delivered.</p><button className="secondary" disabled={busy} onClick={()=>void resolve(message,"mark_sent")}>I confirmed it was sent</button><button className="secondary" disabled={busy} onClick={()=>void resolve(message,"cancel")}>Cancel this attempt</button></div>}<details><summary>Interpretation evidence</summary><pre>{JSON.stringify(message.interpretation,null,2)}</pre></details></article>)}
      <details><summary>Operational audit</summary>{selected.events?.map(event=><article key={event.id}><strong>{event.kind}</strong><pre>{JSON.stringify(event.details,null,2)}</pre></article>)}</details>
    </Modal>}
  </section>;
}
