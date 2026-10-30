"use client";
import {useEffect,useEffectEvent,useState} from "react";
import {api,ApiError} from "./api";

type Status={configured:boolean;bot_username:string|null;worker:string;linked:boolean;failed_inputs?:number;
  allow_memory_write:boolean;proactive_enabled:boolean;
  deliveries:{id:string;state:string;reason:string;attempts:number;message_id:string|null;action_id:string|null}[]};

export default function Telegram({onExpired}:{onExpired:()=>void}){
  const [open,setOpen]=useState(false);const [data,setData]=useState<Status|null>(null);
  const [write,setWrite]=useState(false);const [proactive,setProactive]=useState(false);
  const [link,setLink]=useState<{url:string;expires_at:string}|null>(null);
  const [busy,setBusy]=useState(false);const [error,setError]=useState("");
  function failure(cause:unknown){if(cause instanceof ApiError&&cause.status===401)onExpired();setError(cause instanceof ApiError?cause.message:"Telegram operation failed.");}
  async function load(syncPermissions=false){try{const current=await api<Status>("/api/telegram");setData(current);if(syncPermissions){setWrite(current.allow_memory_write);setProactive(current.proactive_enabled);}if(current.linked)setLink(null);}catch(cause){failure(cause);}}
  const refresh=useEffectEvent(()=>load());
  useEffect(()=>{if(!open)return;const timer=setInterval(()=>{if(document.visibilityState==="visible")void refresh();},5000);return()=>clearInterval(timer);},[open]);
  async function action(kind:"link"|"permissions"|"disconnect"){
    setBusy(true);setError("");
    try{
      if(kind==="disconnect"){await api("/api/telegram/link",{method:"DELETE"});setLink(null);}
      else {
        const result=await api<{url:string;expires_at:string}>(`/api/telegram/${kind}`,{method:kind==="link"?"POST":"PATCH",body:JSON.stringify({allow_memory_write:write,proactive_enabled:proactive})});
        if(kind==="link")setLink(result);
      }
      await load(true);
    }catch(cause){failure(cause);}finally{setBusy(false);}
  }
  return <section className="today-section" aria-labelledby="telegram-heading">
    <div className="section-title"><h2 id="telegram-heading">Telegram</h2><button className="secondary" aria-expanded={open} onClick={()=>{setOpen(!open);if(!open)void load(true);}}>{open?"Close Telegram":"Open Telegram"}</button></div>
    {open&&<div className="card">
      <p>Private owner DMs share this conversation, memory and personality. Groups and other users are ignored.</p>
      {error&&<p role="alert">{error}</p>}
      {data&&<>
        <p>{data.linked?"Your Telegram account is linked.":"No Telegram account linked."} Worker: {data.worker.replaceAll("_"," ")}</p>
        <p>Saved permissions: memory updates {data.allow_memory_write?"enabled":"off"}; proactive DMs {data.proactive_enabled?"enabled":"paused"}.</p>
        {Boolean(data.failed_inputs)&&<p role="alert">{data.failed_inputs} message(s) could not be processed after bounded retries. Check the web conversation before sending a new request.</p>}
        {data.bot_username&&<p>Bot: @{data.bot_username}</p>}
        {!data.configured&&<p>Set TELEGRAM_BOT_TOKEN in the private server environment and start the Telegram worker.</p>}
        <label className="check"><input type="checkbox" checked={write} onChange={e=>setWrite(e.target.checked)}/>Allow my linked DMs to save and update memories, projects and commitments until I revoke this permission</label>
        <label className="check"><input type="checkbox" checked={proactive} onChange={e=>setProactive(e.target.checked)}/>Deliver my enabled check-ins and routines to my Telegram DM</label>
        <p>Proactive delivery also respects your personal autonomy settings and quiet hours. /stop pauses Telegram check-ins; disconnect revokes access.</p>
        {data.linked?<div className="actions"><button disabled={busy} onClick={()=>void action("permissions")}>Save Telegram permissions</button><button disabled={busy} className="secondary" onClick={()=>void action("disconnect")}>Disconnect Telegram</button></div>:<button disabled={busy||data.worker!=="recent_heartbeat"} onClick={()=>void action("link")}>Create private linking invitation</button>}
        {link&&!data.linked&&<p role="status"><a href={link.url} target="_blank" rel="noreferrer">Open your bot and press Start</a>. This private, single-use invitation expires at {new Date(link.expires_at).toLocaleTimeString()}. Do not share it.</p>}
        <details><summary>Telegram delivery history</summary>{data.deliveries.length===0?<p>No deliveries yet.</p>:<ul>{data.deliveries.map(row=><li key={row.id}>{row.state} — {row.reason.replaceAll("_"," ")} · attempts {row.attempts}{row.state==="uncertain"&&<p>Delivery could not be confirmed. It will not be resent automatically; check Telegram and the saved web conversation.</p>}</li>)}</ul>}</details>
      </>}
    </div>}
  </section>;
}
