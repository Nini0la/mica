"use client";
import {FormEvent,useEffect,useMemo,useState} from "react";
import {api,ApiError} from "./api";
import Modal from "./modal";

export type ThreadState="active"|"exploring"|"waiting"|"dormant"|"completed"|"abandoned";
export type ThreadItem={id:string;title:string;description:string;state:ThreadState;importance:number;deadline:string|null;next_action:string;watch:boolean;version:number;created_at:string;derivation:string;kind?:string;why?:string;epistemic_status?:string};
const states:ThreadState[]=["active","exploring","waiting","dormant","completed","abandoned"];

export default function Threads({items:initial,loading,loadFailed,onSessionExpired}:{items:ThreadItem[];loading?:boolean;loadFailed?:boolean;onSessionExpired:()=>void}){
  const [items,setItems]=useState(initial);const [filter,setFilter]=useState<ThreadState|"all">("all");
  const [editing,setEditing]=useState<ThreadItem|null>(null);const [error,setError]=useState("");const [status,setStatus]=useState("");
  const visible=useMemo(()=>items.filter(item=>filter==="all"||item.state===filter),[items,filter]);
  // The shell refreshes this external server snapshot independently.
  // eslint-disable-next-line react-hooks/set-state-in-effect
  useEffect(()=>setItems(initial),[initial]);
  async function save(event:FormEvent<HTMLFormElement>){
    event.preventDefault();if(!editing)return;const form=new FormData(event.currentTarget);const title=String(form.get("title")??"").trim();
    if(!title){setError("Title is required");return;}setError("");setStatus("Saving…");
    const payload={title,description:String(form.get("description")??"").trim(),kind:String(form.get("kind")??"").trim(),why:String(form.get("why")??"").trim(),epistemic_status:String(form.get("epistemic_status")??"").trim(),state:String(form.get("state")),importance:Number(form.get("importance")),deadline:String(form.get("deadline")||"")||null,next_action:String(form.get("next_action")??"").trim(),watch:form.get("watch")==="on",version:editing.version};
    try{const updated=await api<ThreadItem>(`/api/threads/${editing.id}`,{method:"PATCH",body:JSON.stringify(payload)});setItems(all=>all.map(x=>x.id===updated.id?updated:x));setEditing(null);setStatus("Thread updated");}
    catch(cause){if(cause instanceof ApiError&&cause.status===401)onSessionExpired();setError(cause instanceof ApiError&&cause.status===409?"This thread changed somewhere else. Close and reopen it to use the latest version.":"Could not save this thread. Try again.");setStatus("");}
  }
  return <section className="view" aria-labelledby="threads-heading">
    <header className="view-header"><div><span className="eyebrow">Your landscape</span><h1 id="threads-heading">Threads</h1><p>Ideas and work worth carrying forward.</p></div></header>
    <div className="filter-row" aria-label="Filter threads">{(["all",...states] as const).map(state=><button className={filter===state?"chip active":"chip"} aria-pressed={filter===state} key={state} onClick={()=>setFilter(state)}>{state[0].toUpperCase()+state.slice(1)}</button>)}</div>
    <p className="sr-status" aria-live="polite">{status}</p>
    {loading?<div className="empty" role="status" aria-live="polite" aria-busy="true"><h2>Loading threads…</h2><p>Retrieving your current landscape.</p></div>:loadFailed&&visible.length===0?null:visible.length===0?<div className="empty"><h2>Nothing here yet</h2><p>Threads derived from your conversations will appear here.</p></div>:<ul className="card-list">{visible.map(item=><li className="card thread-card" key={item.id}><div className="card-top"><span className={`badge ${item.state}`}>{item.state}</span>{item.watch&&<span className="muted">Watching</span>}</div><h2>{item.title}</h2>{item.description&&<p>{item.description}</p>}<dl className="meta"><div><dt>Importance</dt><dd>{item.importance}/3</dd></div>{item.deadline&&<div><dt>Deadline</dt><dd>{item.deadline}</dd></div>}</dl>{item.next_action&&<p><strong>Next:</strong> {item.next_action}</p>}<p className="provenance">Source: {item.derivation}</p><button className="secondary" onClick={()=>{setEditing(item);setError("")}} aria-label={`Edit ${item.title}`}>Inspect & edit</button></li>)}</ul>}
    {editing&&<Modal labelledBy="edit-thread" onClose={()=>setEditing(null)}><h2 id="edit-thread">Edit thread</h2><form onSubmit={save}><label>Title<input data-autofocus name="title" defaultValue={editing.title} maxLength={500}/></label><label>Description<textarea name="description" defaultValue={editing.description} maxLength={2000}/></label>
      <label>Kind<select name="kind" defaultValue={editing.kind??"thread"}>{["thread","project","idea"].map(kind=><option key={kind}>{kind}</option>)}</select></label>
      <label>Why this matters<textarea name="why" defaultValue={editing.why??""} maxLength={2000}/></label>
      <label>Epistemic status<select name="epistemic_status" defaultValue={editing.epistemic_status??"fact"}>{["fact","inference","hypothesis"].map(status=><option key={status}>{status}</option>)}</select></label><p className="muted">Fact means user-stated, not independently verified.</p>
      <label>State<select name="state" defaultValue={editing.state}>{states.map(state=><option key={state}>{state}</option>)}</select></label><label>Importance<input name="importance" type="number" min="0" max="3" defaultValue={editing.importance}/></label><label>Deadline<input name="deadline" type="date" defaultValue={editing.deadline??""}/></label><label>Next action<input name="next_action" defaultValue={editing.next_action} maxLength={500}/></label><label className="check"><input name="watch" type="checkbox" defaultChecked={editing.watch}/> Watch this thread</label>{error&&<p role="alert" className="error">{error}</p>}<div className="actions"><button type="button" className="secondary" onClick={()=>setEditing(null)}>Cancel</button><button>Save changes</button></div></form></Modal>}
  </section>;
}
