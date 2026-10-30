"use client";

import {useEffect, useEffectEvent, useState} from "react";
import {api, ApiError} from "./api";
import Modal from "./modal";
import "./response-context.css";

type InputMessage = {role: string; content?: unknown; [key: string]: unknown};
type ModelCall = {
  provider?: string;
  index: number; phase: string; status: string; request_scope: string;
  request: {messages: InputMessage[]; tools?: unknown[]; [key: string]: unknown};
  sections: {message_index: number; kind: string}[];
  context_manifest: Record<string, unknown>; started_at: string;
  request_chars: number; approximate_input_tokens: number; elapsed_ms: number;
  response: {content: string; tool_calls: unknown[]} | null;
  usage: {model: string; input_tokens: number | null; output_tokens: number | null;
    cost_usd: number | null; latency_ms: number} | null;
  error: string | null;
};
type Execution = {after_call_index: number; tool_call_id: string; name: string;
  arguments: string; result: unknown};
export type ContextInspection = {
  message_id: string; response: string; created_at: string; status: string;
  policy: unknown; intervention_type: string | null;
  execution_status?:string|null;event_id?:string|null;
  execution_evidence?:unknown;
  trace: {schema_version: number; calls: ModelCall[]; tool_executions: Execution[];
    final_call_index: number | null; output_origin: string;
    transitions?:unknown[];effects?:unknown[]} | null;
};

function text(value: unknown): string {
  return typeof value === "string" ? value : JSON.stringify(value, null, 2) ?? "";
}

function Json({value}: {value: unknown}) {
  return <pre className="context-json">{text(value)}</pre>;
}

function ContextData({message}: {message: InputMessage}) {
  let parsed: Record<string, unknown> | null = null;
  const content = text(message.content);
  try {
    const candidate: unknown = JSON.parse(content.slice(content.indexOf("\n") + 1));
    if(candidate && typeof candidate === "object" && !Array.isArray(candidate))
      parsed = candidate as Record<string, unknown>;
  } catch { /* Raw text is still available when a future context format is unknown. */ }
  if(!parsed) return <Json value={message.content}/>;
  return <div className="context-groups">
    {parsed.profile !== undefined && <details><summary>Pet / profile context</summary><Json value={parsed.profile}/></details>}
    {Array.isArray(parsed.recent_conversation) && <section><h4>Recent conversation included</h4>
      {parsed.recent_conversation.length === 0 ? <p className="muted">No prior conversation included.</p> :
        parsed.recent_conversation.map((entry, i) => {
          const item = entry as Record<string, unknown>;
          return <article className="context-record" key={i}><strong>{text(item.role)}</strong>
            <Json value={item.content ?? item}/></article>;
        })}</section>}
    {Array.isArray(parsed.records) && <section><h4>Retrieved records ({parsed.records.length})</h4>
      {parsed.records.length === 0 ? <p className="muted">No personal records included.</p> :
        parsed.records.map((entry, i) => {
          const item = entry as Record<string, unknown>;
          return <details key={i} className="context-record" open><summary>
            {text(item.kind ?? item.type)} · {text(item.title ?? item.id)}
          </summary><Json value={item}/></details>;
        })}</section>}
    {parsed.feedback != null && <section><h4>Previous assistance feedback</h4><Json value={parsed.feedback}/></section>}
    {parsed.conversation_state!=null&&<section><h4>Pending activities, proposals and reminder plans</h4><Json value={parsed.conversation_state}/></section>}
    <details><summary>Exact context message, including its wrapper</summary><Json value={message.content}/></details>
  </div>;
}

const labels: Record<string, string> = {
  user_message: "Your message", instructions: "System instructions",
  retrieved_context: "Personal context", tool_results: "Tool results sent to the model",
};

function CallDetail({call, executions}: {call: ModelCall; executions: Execution[]}) {
  const order = ["user_message", "instructions", "retrieved_context", "tool_results"];
  const sections = [...call.sections].sort((a, b) => order.indexOf(a.kind) - order.indexOf(b.kind));
  const settings = Object.fromEntries(Object.entries(call.request).filter(([key]) => !["messages", "tools"].includes(key)));
  return <div className="context-call">
    <dl className="context-metrics">
      <div><dt>Provider</dt><dd>{call.provider ?? "Not recorded"}</dd></div>
      <div><dt>Model</dt><dd>{call.usage?.model ?? text(call.request.model ?? "Not reported")}</dd></div>
      <div><dt>Call status</dt><dd>{call.status}</dd></div>
      <div><dt>Input tokens</dt><dd>{call.usage?.input_tokens ?? `≈ ${call.approximate_input_tokens} (character estimate)`}</dd></div>
      <div><dt>Output tokens</dt><dd>{call.usage?.output_tokens ?? "Not reported"}</dd></div>
      <div><dt>Latency</dt><dd>{call.elapsed_ms} ms</dd></div>
      <div><dt>Estimated cost</dt><dd>{call.usage?.cost_usd == null ? "Not configured / reported" : `$${call.usage.cost_usd.toFixed(6)}`}</dd></div>
    </dl>
    {call.error && <p className="error">Call failed: {call.error}. These inputs were prepared for the attempt; successful provider processing is not confirmed.</p>}
    {call.response&&call.response.tool_calls.length===0&&<p>No tool calls were requested on this model call.</p>}
    {sections.map(section => {
      const message = call.request.messages[section.message_index];
      if(!message) return null;
      const heading = labels[section.kind] ?? section.kind;
      return <section className="context-section" key={section.message_index}>
        <h3>{heading}</h3><p className="muted">Request message {section.message_index + 1} · actual role: {message.role}</p>
        {section.kind === "retrieved_context" ? <ContextData message={message}/> :
          section.kind === "instructions" ? <details><summary>Read the instructions used for this call</summary><Json value={message.content}/></details> :
            <Json value={message.content}/>}
      </section>;
    })}
    <section className="context-section"><h3>Available tools ({call.request.tools?.length ?? 0})</h3>
      {call.request.tools?.length ? <details><summary>Tool definitions sent to the model</summary><Json value={call.request.tools}/></details> :
        <p className="muted">No tools offered on this call.</p>}</section>
    <details className="context-section"><summary>Why records were selected or excluded</summary><Json value={call.context_manifest}/></details>
    {executions.length > 0 && <section className="context-section"><h3>Tool executions after call {call.index}</h3>
      <p className="muted">These are actual execution results. The next call may receive a refreshed context and a smaller receipt instead of this result verbatim.</p>
      {executions.map(execution => <details key={execution.tool_call_id} open><summary>{execution.name} · {execution.tool_call_id}</summary>
        <h4>Arguments</h4><Json value={execution.arguments}/><h4>Execution result</h4><Json value={execution.result}/>
      </details>)}</section>}
    <details className="context-section"><summary>Model output before application formatting</summary>
      {call.response ? <><Json value={call.response.content}/>{call.response.tool_calls.length > 0 && <><h4>Requested tool calls</h4><Json value={call.response.tool_calls}/></>}</> : <p>No model output was recorded.</p>}
    </details>
    <details className="context-section"><summary>Model request settings</summary><Json value={settings}/></details>
    <details className="context-section"><summary>Raw request JSON · original message order</summary>
      <p className="muted">{call.request_scope === "provider_json_body" ? "Captured provider request body, excluding transport headers and credentials." : "Captured gateway inputs; provider-specific request settings were unavailable."}</p>
      <Json value={call.request}/>
    </details>
  </div>;
}

export default function ResponseContext({messageId, onClose, onSessionExpired}: {
  messageId: string; onClose: () => void; onSessionExpired: () => void;
}) {
  const [data, setData] = useState<ContextInspection | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [attempt, setAttempt] = useState(0);
  const [selected, setSelected] = useState<number | null>(null);
  const [confirmClear, setConfirmClear] = useState(false);
  const [clearing, setClearing] = useState(false);
  const expireSession = useEffectEvent(onSessionExpired);
  useEffect(() => {
    let active = true;
    api<ContextInspection>(`/api/chat/messages/${encodeURIComponent(messageId)}/context`, {cache: "no-store"})
      .then(result => {
        if(!active) return;
        setData(result); setSelected(result.trace?.final_call_index ?? result.trace?.calls.at(-1)?.index ?? null);
        setLoading(false);
      }).catch(cause => {
        if(!active) return;
        if(cause instanceof ApiError && cause.status === 401) expireSession();
        setError(cause instanceof ApiError && cause.status === 404 ? "This response is unavailable." : "Could not load response context. Try again.");
        setLoading(false);
      });
    return () => {active = false};
  }, [messageId, attempt]);
  async function clear() {
    setClearing(true); setError("");
    try {
      await api(`/api/chat/messages/${encodeURIComponent(messageId)}/context`, {method: "DELETE"});
      setData(current => current ? {...current, status: "cleared_by_user", trace: null} : null);
      setConfirmClear(false);
    } catch(cause) {
      if(cause instanceof ApiError && cause.status === 401) onSessionExpired();
      setError("Could not clear this snapshot. Try again.");
    } finally {setClearing(false)}
  }
  const call = data?.trace?.calls.find(item => item.index === selected);
  return <Modal labelledBy="response-context-title" onClose={onClose} className="response-inspector">
    <header className="context-heading"><div><span className="eyebrow">Response-level inspection</span><h2 id="response-context-title">Response context</h2></div>
      <button data-autofocus className="secondary" onClick={onClose}>Close</button></header>
    <p>Historical inputs captured when this response was generated. This shows what the model received, not proof of which input caused its answer or hidden model reasoning.</p>
    {loading && <p role="status">Loading captured context…</p>}
    {error && <div className="error-row" role="alert"><span>{error}</span><button className="secondary" onClick={() => {setError(""); setLoading(true); setAttempt(n => n + 1)}}>Retry context</button></div>}
    {data && <>
      <details className="context-section"><summary>Selected assistant response</summary><Json value={data.response}/></details>
      {data.execution_evidence!=null&&<details className="context-section" open><summary>Current execution and delivery records</summary><p>Persisted action/provider evidence, separate from the historical model-input snapshot.</p><Json value={data.execution_evidence}/></details>}
      {data.status !== "captured" || !data.trace ? <p role="status" className="context-empty">{
        data.status === "cleared_by_memory_deletion" ? "This snapshot was cleared when retained memory was deleted, so deleted content cannot survive in captured prompts." :
        data.status === "cleared_by_user" ? "This context snapshot has been cleared. The conversation message is unchanged." :
        "Context was not recorded for this response. It may predate capture or come from a deterministic skill. We cannot reconstruct its exact inputs retrospectively."
      }</p> : <>
        <p className="muted">Output origin: {data.trace.output_origin.replaceAll("_", " ")}. Snapshots stay in your account and are never used as memory retrieval sources.</p>
        {data.execution_status&&<p>Execution outcome: {data.execution_status} · event {data.event_id}</p>}
        {Boolean(data.trace.effects?.length)&&<details className="context-section" open><summary>Persisted effect receipts</summary><p>These receipts were saved with this response. They do not assert external delivery.</p><Json value={data.trace.effects}/></details>}
        {Boolean(data.trace.transitions?.length)&&<details className="context-section" open><summary>Workflow transitions</summary><Json value={data.trace.transitions}/></details>}
        {data.trace.calls.length === 0 ? <section><h3>No model call</h3><p>This response was produced by application policy, so there was no LLM context window.</p><Json value={data.policy}/></section> : <>
          <div className="context-call-picker" aria-label="Model calls">{data.trace.calls.map(item => <button
            key={item.index} className={selected === item.index ? "chip active" : "chip"} aria-pressed={selected === item.index} onClick={() => setSelected(item.index)}>
            Call {item.index}{data.trace?.final_call_index === item.index ? " · Final response" : " · " + item.phase.replaceAll("_", " ")}
          </button>)}</div>
          {call && <CallDetail call={call} executions={data.trace.tool_executions.filter(item => item.after_call_index === call.index)}/>}
          <details className="context-section"><summary>Assistance decision and permissions</summary><Json value={data.policy}/></details>
        </>}
        <footer className="context-section"><p className="muted">Captured inputs contain personal content. Deleting any retained memory clears all captured response inputs for your account; correction keeps these historical snapshots for analysis.</p>
          {confirmClear ? <div className="actions"><span>Clear this captured context?</span><button className="secondary" disabled={clearing} onClick={() => setConfirmClear(false)}>Cancel</button><button className="danger-button" disabled={clearing} onClick={() => void clear()}>Clear snapshot</button></div> :
            <button className="text-button danger" onClick={() => setConfirmClear(true)}>Remove captured context</button>}
        </footer>
      </>}
    </>}
  </Modal>;
}
