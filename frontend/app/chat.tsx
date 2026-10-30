"use client";
import {FormEvent,useEffect,useLayoutEffect,useRef,useState} from "react";
import {api,ApiError} from "./api";
import Companion from "./companion";
import ResponseContext from "./response-context";
export type Message={id:string;role:"user"|"assistant";content:string;created_at?:string;proactive?:boolean};
export type ChatPage={items:Message[];has_more:boolean;older_cursor:string|null;limit:number;returned_count:number};
type Pending={content:string;eventId:string;accepted:boolean;allowMemoryWrite:boolean;activeThreadId:string|null};
const EMPTY_MESSAGES:Message[]=[];

function latestPath(cursor?:string|null){return `/api/chat/messages?mode=latest&limit=100${cursor?`&cursor=${encodeURIComponent(cursor)}`:""}`}
function appendNew(current:Message[],latest:Message[]){
  const seen=new Set(current.map(message=>message.id));
  return [...current,...latest.filter(message=>!seen.has(message.id))];
}

export default function Chat({initialMessages=EMPTY_MESSAGES,initialOlderCursor=null,initialHasMore=false,onSessionExpired=()=>undefined,onChanged=()=>undefined}:{initialMessages?:Message[];initialOlderCursor?:string|null;initialHasMore?:boolean;onSessionExpired?:()=>void;onChanged?:()=>void}){
  const [inspecting,setInspecting]=useState<string|null>(null);
  const [allowMemoryWrite,setAllowMemoryWrite]=useState(false);const [activeThreadId,setActiveThreadId]=useState<string|null>(null);
  const [petName,setPetName]=useState("Mystery Pet");
  const paginationRestore=useRef<{viewport:HTMLDivElement|null;height:number;top:number;focusId?:string|null}|null>(null);
  const [messages,setMessages]=useState(initialMessages);const [text,setText]=useState("");const [sending,setSending]=useState(false);const [error,setError]=useState("");const [pending,setPending]=useState<Pending|null>(null);const [olderCursor,setOlderCursor]=useState(initialOlderCursor);const [hasMore,setHasMore]=useState(initialHasMore);const [loadingEarlier,setLoadingEarlier]=useState(false);const [paginationAnnouncement,setPaginationAnnouncement]=useState("");const inputRef=useRef<HTMLTextAreaElement>(null);const messagesRef=useRef<HTMLDivElement>(null);const headingRef=useRef<HTMLHeadingElement>(null);const messageRefs=useRef(new Map<string,HTMLElement>());const loadedEarlier=useRef(false);
  // Parent data is authoritative for the newest page after reconciliation and scheduled actions.
  // eslint-disable-next-line react-hooks/set-state-in-effect
  useEffect(()=>{setMessages(current=>appendNew(current,initialMessages));if(!loadedEarlier.current){setOlderCursor(initialOlderCursor);setHasMore(initialHasMore)}},[initialMessages,initialOlderCursor,initialHasMore]);
  // Restore pagination only after the new message nodes have committed, not on a timer.
  useLayoutEffect(()=>{
    const restore=paginationRestore.current;if(!restore)return;paginationRestore.current=null;
    if(restore.viewport)restore.viewport.scrollTop=restore.viewport.scrollHeight-restore.height+restore.top;
    if(restore.focusId!==undefined)(messageRefs.current.get(restore.focusId??"")??headingRef.current)?.focus({preventScroll:true});
  },[messages]);
  async function deliver(content:string,retry:Pending|null=null){
    const trimmed=content.trim();if(!trimmed||sending||(!retry&&pending))return;const attempt=retry??{content:trimmed,eventId:crypto.randomUUID(),accepted:false,allowMemoryWrite,activeThreadId};
    if(!retry){setMessages(all=>[...all,{id:`pending-${attempt.eventId}`,role:"user",content:trimmed,created_at:new Date().toISOString()}]);setText("");setAllowMemoryWrite(false);}
    setSending(true);setError("");setPending(attempt);
    try{
      if(!attempt.accepted){await api("/api/chat/messages",{method:"POST",body:JSON.stringify({content:attempt.content,event_id:attempt.eventId,allow_memory_write:attempt.allowMemoryWrite,active_thread_id:attempt.activeThreadId})});attempt.accepted=true;setPending({...attempt});}
      const history=await api<ChatPage>(latestPath());
      setMessages(current=>appendNew(current.filter(message=>message.id!==`pending-${attempt.eventId}`),history.items));
      if(!loadedEarlier.current){setOlderCursor(history.older_cursor);setHasMore(history.has_more)}
      setPending(null);onChanged();
    }catch(cause){
      if(cause instanceof ApiError&&cause.status===401){setError("Your session expired. Sign in again to continue.");onSessionExpired();}
      else setError(attempt.accepted?"Message accepted, but conversation refresh failed. Retry to reconcile safely.":"Delivery is uncertain. Retry safely with the same event ID.");
    }finally{setSending(false)}
  }
  async function loadEarlier(){
    if(!olderCursor||loadingEarlier)return;const viewport=messagesRef.current;const beforeHeight=viewport?.scrollHeight??0;const beforeTop=viewport?.scrollTop??0;setLoadingEarlier(true);setError("");setPaginationAnnouncement("");
    try{const page=await api<ChatPage>(latestPath(olderCursor));const seenBefore=new Set(messages.map(message=>message.id));const firstNewId=page.items.find(message=>!seenBefore.has(message.id))?.id;const finalPage=!page.has_more||!page.older_cursor;paginationRestore.current={viewport,height:beforeHeight,top:beforeTop,focusId:finalPage?firstNewId??null:undefined};setMessages(current=>{const seen=new Set(current.map(message=>message.id));return [...page.items.filter(message=>!seen.has(message.id)),...current]});setOlderCursor(page.older_cursor);setHasMore(page.has_more);setPaginationAnnouncement(finalPage?"Earlier messages loaded. Start of conversation reached.":"Earlier messages loaded.");loadedEarlier.current=true;}
    catch(cause){if(cause instanceof ApiError&&cause.status===401){setError("Your session expired. Sign in again to continue.");onSessionExpired();}else setError("Earlier messages could not be loaded. Try again.")}
    finally{setLoadingEarlier(false)}
  }
  function send(event:FormEvent){event.preventDefault();void deliver(text)}
  return <section className="view chat-view" aria-labelledby="chat-heading"><header className="view-header"><div><span className="eyebrow">A shared conversation</span><h1 ref={headingRef} id="chat-heading" tabIndex={-1}>Chat</h1><p>Your Mystery Pet companion, backed by Continuity. Talk naturally; only explicit, useful details are retained.</p></div></header>
    <Companion thinking={sending} refreshKey={messages.filter(message=>message.role==="assistant").at(-1)?.id} activeThreadId={activeThreadId} onSelect={setActiveThreadId} onProfile={profile=>setPetName(profile.name)} onDraft={draft=>{setText(current=>current?`${current}\n${draft}`.slice(0,4000):draft.slice(0,4000));inputRef.current?.focus()}} onSessionExpired={onSessionExpired}/>
    <p className="visually-hidden" role="status" aria-live="polite">{paginationAnnouncement}</p>
    {hasMore&&olderCursor&&<div className="load-earlier"><button type="button" className="secondary" disabled={loadingEarlier} onClick={()=>void loadEarlier()}>{loadingEarlier?"Loading earlier messages…":"Load earlier messages"}</button></div>}
    <div ref={messagesRef} className="messages" aria-live="polite" aria-busy={sending||loadingEarlier}>{messages.length===0?<div className="empty"><h2>Start where you are</h2><p>Share a plan, a commitment, or something you want to think through.</p></div>:messages.map(message=><article ref={node=>{if(node)messageRefs.current.set(message.id,node);else messageRefs.current.delete(message.id)}} tabIndex={-1} className={`message ${message.role}`} key={message.id}><header><span>{message.role==="assistant"?petName:"You"}</span>{message.proactive&&<span className="badge">Proactive</span>}</header><p>{message.content}</p>{message.created_at&&<time dateTime={message.created_at}>{new Date(message.created_at).toLocaleTimeString([],{hour:"2-digit",minute:"2-digit"})}</time>}{message.role==="assistant"&&<button className="text-button" onClick={()=>setInspecting(message.id)}>View context</button>}</article>)}</div>
    {inspecting&&<ResponseContext key={inspecting} messageId={inspecting} onClose={()=>setInspecting(null)} onSessionExpired={onSessionExpired}/>}
    {error&&<div className="error-row" role="alert"><span>{error}</span>{pending&&<button className="secondary" onClick={()=>deliver(pending.content,pending)}>Retry</button>}</div>}
    <form className="composer" onSubmit={send}><label htmlFor="message" className="visually-hidden">Message</label><textarea ref={inputRef} id="message" maxLength={4000} rows={3} value={text} onChange={event=>setText(event.target.value)} placeholder={`Message ${petName}…`}/><label className="check memory-permission"><input type="checkbox" checked={allowMemoryWrite} disabled={sending||Boolean(pending)} onChange={event=>setAllowMemoryWrite(event.target.checked)}/>Allow saving or updating this idea, project, or commitment</label><small>For this message only. Unchecked by default; not a permanent permission.</small><div><small>{text.length} / 4000</small><button disabled={sending||Boolean(pending)||!text.trim()}>{sending?"Sending…":"Send"}</button></div></form>
  </section>;
}
