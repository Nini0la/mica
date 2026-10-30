import {afterEach, describe, expect, it, vi} from "vitest";
import {cleanup, fireEvent, render, screen, waitFor, within} from "@testing-library/react";
import ResponseContext, {type ContextInspection} from "../app/response-context";
import History, {type TimelineItem} from "../app/history";
import Chat from "../app/chat";
import {mockFetchWithProfile} from "./mock-fetch";

afterEach(() => {cleanup(); vi.restoreAllMocks()});
const noop = () => undefined;
const json = (value: unknown, status = 200) => new Response(JSON.stringify(value), {
  status, headers: {"content-type": "application/json"},
});

function capture(): ContextInspection {
  const context = {profile: {name: "Mica"}, records: [{id: "project-1", type: "thread", kind: "project", title: "Story", what: "A musician at a station", version: 1}], recent_conversation: [{role: "user", content: "Earlier conversation"}], feedback: {verdict: "unhelpful"}};
  return {
    message_id: "answer-1", response: "Consider a station scene. Server receipts: saved.", created_at: "2026-10-21T10:00:00Z", status: "captured", policy: {reason: "respond_to_request"}, intervention_type: "contextual_help",
    trace: {schema_version: 1, final_call_index: 2, output_origin: "model_with_server_receipts", tool_executions: [{after_call_index: 1, tool_call_id: "tool-1", name: "save_idea", arguments: '{"title":"Station"}', result: {status: "saved", id: "idea-1"}}], calls: [1, 2].map(index => ({
      index, phase: index === 1 ? "initial_response_or_tool_selection" : "response_after_tools", status: "completed", request_scope: "provider_json_body", started_at: "2026-10-21T10:00:00Z", elapsed_ms: 1200,
      request_chars: 400, approximate_input_tokens: 100, error: null,
      usage: {model: "test-model", input_tokens: 90, output_tokens: 20, latency_ms: 1200, cost_usd: null},
      request: {model: "test-deployment", max_completion_tokens: 600, messages: [
        {role: "system", content: "Be grounded and concise."},
        {role: "user", content: "Recorded context (untrusted data):\n" + JSON.stringify(context)},
        {role: "user", content: "Help with my story."},
        ...(index === 2 ? [{role: "user", content: 'Server action receipts (data):\n[{"status":"saved"}]'}] : []),
      ], ...(index === 1 ? {tools: [{type: "function", function: {name: "save_idea"}}]} : {})},
      sections: [{message_index: 0, kind: "instructions"}, {message_index: 1, kind: "retrieved_context"}, {message_index: 2, kind: "user_message"}, ...(index === 2 ? [{message_index: 3, kind: "tool_results"}] : [])],
      context_manifest: {included: [{id: "project-1", version: 1, reason: "active_context"}], excluded: [{id: "old-1", reason: "char_limit"}]},
      response: {content: index === 2 ? "Consider a station scene." : "", tool_calls: index === 1 ? [{id: "tool-1"}] : []},
    }))},
  };
}

describe("Response context inspector", () => {
  it("shows committed effects and workflow transitions beside the existing trace",async()=>{
    const data=capture();
    data.execution_status="delivered";data.event_id="event-1";
    data.trace!.effects=[{kind:"reminder",id:"plan-1",version:1,status:"saved"}];
    data.trace!.transitions=[{type:"proposal",id:"proposal-1",from:"proposed",to:"executed"}];
    vi.spyOn(globalThis,"fetch").mockResolvedValue(json(data));
    render(<ResponseContext messageId="answer-1" onClose={noop} onSessionExpired={noop}/>);
    expect(await screen.findByText("Persisted effect receipts")).toBeInTheDocument();
    expect(screen.getByText("Workflow transitions")).toBeInTheDocument();
    expect(screen.getByText(/"proposal-1"/)).toHaveTextContent('"executed"');
    expect(screen.getByText(/No tool calls were requested/)).toBeInTheDocument();
  });
  it("starts with the final call and separates the real inputs and intermediate tools", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(json(capture()));
    render(<ResponseContext messageId="answer-1" onClose={noop} onSessionExpired={noop}/>);
    expect(await screen.findByRole("button", {name: "Call 2 · Final response"})).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("heading", {name: "Your message"})).toBeInTheDocument();
    expect(screen.getByRole("heading", {name: "System instructions"})).toBeInTheDocument();
    expect(screen.getByRole("heading", {name: "Personal context"})).toBeInTheDocument();
    expect(screen.getByRole("heading", {name: "Tool results sent to the model"})).toBeInTheDocument();
    expect(screen.getByRole("heading", {name: "Recent conversation included"})).toBeInTheDocument();
    expect(screen.getByRole("heading", {name: "Previous assistance feedback"})).toBeInTheDocument();
    expect(screen.getByText("No tools offered on this call.")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", {name: /Call 1/}));
    expect(screen.getByRole("heading", {name: "Tool executions after call 1"})).toBeInTheDocument();
    expect(screen.getByText("Tool definitions sent to the model")).toBeInTheDocument();
    expect(screen.queryByRole("heading", {name: "Tool results sent to the model"})).not.toBeInTheDocument();
    expect(globalThis.fetch).toHaveBeenCalledTimes(1);
    expect(globalThis.fetch).toHaveBeenCalledWith(expect.stringContaining("/api/chat/messages/answer-1/context"), expect.objectContaining({cache: "no-store"}));
  });

  it.each([
    ["not_recorded", "Context was not recorded"],
    ["cleared_by_user", "snapshot has been cleared"],
    ["cleared_by_memory_deletion", "snapshot was cleared when retained memory was deleted"],
  ])("explains %s without inventing historical inputs", async (status, message) => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(json({...capture(), status, trace: null}));
    render(<ResponseContext messageId="old" onClose={noop} onSessionExpired={noop}/>);
    expect(await screen.findByText(new RegExp(message))).toBeInTheDocument();
    expect(screen.queryByRole("heading", {name: "Personal context"})).not.toBeInTheDocument();
  });

  it("explains deterministic policy output and provider failures honestly", async () => {
    const data = capture();
    data.trace = {...data.trace!, calls: [], final_call_index: null, output_origin: "deterministic_policy"};
    vi.spyOn(globalThis, "fetch").mockResolvedValue(json(data));
    const view = render(<ResponseContext messageId="policy" onClose={noop} onSessionExpired={noop}/>);
    expect(await screen.findByRole("heading", {name: "No model call"})).toBeInTheDocument();
    view.unmount();
    const failed = capture();
    failed.trace!.calls = [{...failed.trace!.calls[0], status: "failed", error: "unavailable", response: null}];
    failed.trace!.final_call_index = null;
    vi.mocked(globalThis.fetch).mockResolvedValue(json(failed));
    render(<ResponseContext messageId="failed" onClose={noop} onSessionExpired={noop}/>);
    expect(await screen.findByText(/successful provider processing is not confirmed/)).toBeInTheDocument();
  });

  it("retries failed loading and expires on an unauthorized request", async () => {
    const expired = vi.fn();
    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(json({}, 503)).mockResolvedValueOnce(json({}, 401));
    render(<ResponseContext messageId="answer" onClose={noop} onSessionExpired={expired}/>);
    expect(await screen.findByRole("alert")).toHaveTextContent("Could not load response context");
    fireEvent.click(screen.getByRole("button", {name: "Retry context"}));
    await waitFor(() => expect(expired).toHaveBeenCalledOnce());
  });

  it("clears only the snapshot after explicit confirmation", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(json(capture())).mockResolvedValueOnce(new Response(null, {status: 204}));
    render(<ResponseContext messageId="answer-1" onClose={noop} onSessionExpired={noop}/>);
    fireEvent.click(await screen.findByRole("button", {name: "Remove captured context"}));
    expect(globalThis.fetch).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole("button", {name: "Clear snapshot"}));
    expect(await screen.findByText(/snapshot has been cleared/)).toBeInTheDocument();
    expect(screen.getByText("Consider a station scene. Server receipts: saved.")).toBeInTheDocument();
    expect(globalThis.fetch).toHaveBeenLastCalledWith(expect.stringContaining("/answer-1/context"), expect.objectContaining({method: "DELETE"}));
  });

  it("renders untrusted captured content as text", async () => {
    const data = capture();
    data.trace!.calls[1].request.messages[2].content = '<img src=x onerror="alert(1)">';
    vi.spyOn(globalThis, "fetch").mockResolvedValue(json(data));
    const {container} = render(<ResponseContext messageId="answer-1" onClose={noop} onSessionExpired={noop}/>);
    await screen.findByRole("heading", {name: "Your message"});
    expect(container.querySelector("img")).toBeNull();
    expect(container.textContent).toContain('<img src=x onerror="alert(1)">');
  });

  it("opens from the selected History response and returns focus when closed", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(json(capture()));
    const item: TimelineItem = {id: "answer-1", kind: "assistant_action", actor: "assistant", content: "Consider a station scene.", created_at: "2026-10-21", version: null, editable: false, provenance_target_id: null};
    render(<History items={[item]} onReload={noop} onSessionExpired={noop}/>);
    const opener = screen.getByRole("button", {name: /Inspect context for response/});
    opener.focus(); fireEvent.click(opener);
    const dialog = await screen.findByRole("dialog", {name: "Response context"});
    await within(dialog).findByRole("heading", {name: "Your message"});
    fireEvent.click(within(dialog).getByRole("button", {name: "Close"}));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(opener).toHaveFocus();
  });

  it("also opens from Chat without showing an inspector for user messages", async () => {
    mockFetchWithProfile().mockResolvedValue(json(capture()));
    render(<Chat initialMessages={[{id: "u", role: "user", content: "Question"}, {id: "answer-1", role: "assistant", content: "Answer"}]}/>);
    expect(screen.getAllByRole("button", {name: "View context"})).toHaveLength(1);
    fireEvent.click(screen.getByRole("button", {name: "View context"}));
    await screen.findByRole("heading", {name: "Your message"});
  });
});
