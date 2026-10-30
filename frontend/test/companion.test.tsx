import {fireEvent,render,screen,waitFor,within} from "@testing-library/react";
import {afterEach,describe,expect,it,vi} from "vitest";
import Companion from "../app/companion";
import Chat from "../app/chat";
import Threads,{type ThreadItem} from "../app/threads";

const profile={name:"Pip",temperament:"calm",version:7,model_mode:"live"};
const plate={id:"project-1",title:"Pocket garden",description:"A windowsill experiment",kind:"project",why:"Make room for green",next_action:"Choose seeds",state:"active",version:3,epistemic_status:"fact",parent_thread_id:null,ideas:[{id:"idea-1",title:"Herb labels",description:"Draw little markers"}],commitments:[{id:"commitment-1",title:"Find pots",state:"open"}]};
const record={id:"a1",message_id:"m1",intervention_type:"creative_support",execution_status:"completed",observed_outcome:"unknown",context_manifest:{policy_version:"context-v1",included:[{id:"project-1",type:"thread",reason:"active_context",sources:[{source_id:"message-1",source_type:"message",content:"SECRET CONTEXT"}]}],excluded:[{id:"message-2",type:"message",reason:"char_limit",content:"SECRET EXCLUDED"}],limits:{max_chars:12000,recent_only:false,unknown:"SECRET LIMIT"},chars:800,approximate_tokens:200,raw_context:"SECRET PROMPT"},policy:{version:"assistance-v1",reason:"respond_to_request",allow_memory_write:true,max_tool_calls:2,prompt:"SECRET POLICY",unknown:{id:"SECRET UNKNOWN"}},usage:[{model:"test-model",input_tokens:20,output_tokens:10,latency_ms:42,cost_usd:0.01,raw:"SECRET USAGE",input:"SECRET INPUT",output:"SECRET OUTPUT"},{input_tokens:null,cost_usd:null}],tool_results:[{tool:"create_commitment",status:"saved",id:"commitment-1",kind:"commitment",version:1,output:"SECRET TOOL",arguments:{title:"SECRET ARGUMENTS"},context:{id:"SECRET TOOL CONTEXT"}},{tool:"update_thread",status:"rejected",reason:"permission_required"}],feedback:[],created_at:"2026-10-15T10:00:00Z"};
const props={activeThreadId:null,onSelect:vi.fn(),onDraft:vi.fn(),onSessionExpired:vi.fn()};
function json(value:unknown,status=200){return new Response(JSON.stringify(value),{status,headers:{"content-type":"application/json"}})}
function deferred<T>(){let resolve!:(value:T)=>void;const promise=new Promise<T>(done=>{resolve=done});return {promise,resolve}}
function mockApi(mode="live"){
  return vi.spyOn(globalThis,"fetch").mockImplementation(async(input,init)=>{
    const path=new URL(String(input)).pathname;
    if(path==="/api/companion")return json(init?.method==="PATCH"?{...profile,...JSON.parse(String(init.body)),version:8}:{...profile,model_mode:mode});
    if(path==="/api/context/plates")return json({items:[plate]});
    if(path==="/api/assistance")return json({items:[record],growth:[{dimension:"support",event_type:"feedback",assessment:"reported",created_at:record.created_at}]});
    if(path.endsWith("/feedback"))return json({id:"f1",...JSON.parse(String(init?.body))});
    if(path==="/api/chat/messages")return init?.method==="POST"?json({}):json({items:[],has_more:false,older_cursor:null,limit:100,returned_count:0});
    throw new Error(`Unexpected request ${path}`);
  });
}
async function open(){fireEvent.click(screen.getByRole("button",{name:"Open companion"}));await screen.findByLabelText("Active project or idea");await waitFor(()=>expect(screen.getByRole("button",{name:"Refresh companion"})).toBeEnabled())}
afterEach(()=>{vi.restoreAllMocks();vi.clearAllMocks()});

describe("Companion",()=>{
  it("loads only the profile on mount and persists name and preset with the User CAS version",async()=>{
    const fetchMock=mockApi();const onProfile=vi.fn();render(<Companion {...props} onProfile={onProfile}/>);
    expect(fetchMock).toHaveBeenCalledOnce();
    expect(String(fetchMock.mock.calls[0][0])).toContain("/api/companion");
    expect(screen.getByRole("img",{name:/resting in a hand, idle/})).toBeInTheDocument();
    expect(screen.getByText(/Model mode not verified/)).toBeInTheDocument();
    expect(await screen.findByText("Pip")).toBeInTheDocument();
    expect(screen.getByText(/Live AI - model tools available/)).toBeInTheDocument();
    expect(screen.queryByLabelText("Active project or idea")).not.toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledOnce();expect(onProfile).toHaveBeenCalledWith(profile);
    await open();fireEvent.click(screen.getByText("Companion preferences"));
    expect(screen.getByLabelText("Name")).toHaveAttribute("maxlength","60");
    fireEvent.change(screen.getByLabelText("Name"),{target:{value:" Moss "}});
    fireEvent.change(screen.getByLabelText("Temperament"),{target:{value:"playful"}});
    fireEvent.click(screen.getByRole("button",{name:"Save companion"}));
    expect(await screen.findByText("Companion preferences saved.")).toBeInTheDocument();
    const patch=fetchMock.mock.calls.find(([,init])=>init?.method==="PATCH");
    expect(JSON.parse(String(patch?.[1]?.body))).toEqual({name:"Moss",temperament:"playful",version:7});
    expect(onProfile).toHaveBeenLastCalledWith({...profile,name:"Moss",temperament:"playful",version:8});
    fireEvent.change(screen.getByLabelText("Temperament"),{target:{value:"curious"}});
    fireEvent.click(screen.getByRole("button",{name:"Save companion"}));
    await waitFor(()=>expect(fetchMock.mock.calls.filter(([,init])=>init?.method==="PATCH")).toHaveLength(2));
    expect(JSON.parse(String(fetchMock.mock.calls.at(-1)?.[1]?.body)).version).toBe(8);
  });

  it("labels deterministic mode honestly and offers no model actions",async()=>{
    mockApi("deterministic");const {rerender}=render(<Companion {...props}/>);
    expect(await screen.findByText(/Deterministic mode - no live AI/)).toBeInTheDocument();
    await open();
    expect(screen.queryByRole("button",{name:"Explore alternatives"})).not.toBeInTheDocument();
    rerender(<Companion {...props} thinking/>);
    expect(screen.getByRole("img",{name:/thinking/})).toHaveClass("thinking");
  });

  it("disables companion fields until opening and saving requests finish",async()=>{
    const fetchMock=mockApi();render(<Companion {...props}/>);await screen.findByText("Pip");
    const loading=deferred<Response>();fetchMock.mockImplementationOnce(()=>loading.promise);
    fireEvent.click(screen.getByRole("button",{name:"Open companion"}));
    fireEvent.click(screen.getByText("Companion preferences"));
    expect(screen.getByLabelText("Name")).toBeDisabled();
    expect(screen.getByLabelText("Temperament")).toBeDisabled();
    expect(screen.getByRole("button",{name:"Save companion"})).toBeDisabled();
    loading.resolve(json({...profile,name:"Server name"}));
    await waitFor(()=>expect(screen.getByLabelText("Name")).toBeEnabled());
    expect(screen.getByLabelText("Name")).toHaveValue("Server name");
    expect(screen.getByLabelText("Temperament")).toBeEnabled();
    fireEvent.change(screen.getByLabelText("Name"),{target:{value:"Moss"}});
    fireEvent.change(screen.getByLabelText("Temperament"),{target:{value:"curious"}});
    const saving=deferred<Response>();fetchMock.mockImplementationOnce(()=>saving.promise);
    fireEvent.click(screen.getByRole("button",{name:"Save companion"}));
    expect(screen.getByLabelText("Name")).toBeDisabled();
    expect(screen.getByLabelText("Temperament")).toBeDisabled();
    saving.resolve(json({...profile,name:"Moss",temperament:"curious",version:8}));
    await screen.findByText("Companion preferences saved.");
    expect(screen.getByLabelText("Name")).toBeEnabled();
    expect(screen.getByLabelText("Name")).toHaveValue("Moss");
    expect(screen.getByLabelText("Temperament")).toHaveValue("curious");
  });

  it("keeps unsaved companion drafts through refresh, close/reopen, and response refresh",async()=>{
    const fetchMock=mockApi();const {rerender}=render(<Companion {...props} refreshKey="m1"/>);await open();
    fireEvent.click(screen.getByText("Companion preferences"));
    fireEvent.change(screen.getByLabelText("Name"),{target:{value:"My draft name"}});
    fireEvent.change(screen.getByLabelText("Temperament"),{target:{value:"playful"}});
    const refresh=deferred<Response>();fetchMock.mockImplementationOnce(()=>refresh.promise);
    fireEvent.click(screen.getByRole("button",{name:"Refresh companion"}));
    expect(screen.getByLabelText("Name")).toBeDisabled();
    refresh.resolve(json({...profile,name:"Changed elsewhere",version:8}));
    await waitFor(()=>expect(screen.getByLabelText("Name")).toBeEnabled());
    expect(screen.getByLabelText("Name")).toHaveValue("My draft name");
    expect(screen.getByLabelText("Temperament")).toHaveValue("playful");
    fireEvent.click(screen.getByRole("button",{name:"Close companion"}));await open();
    fireEvent.click(screen.getByText("Companion preferences"));
    expect(screen.getByLabelText("Name")).toHaveValue("My draft name");
    const before=fetchMock.mock.calls.length;
    rerender(<Companion {...props} refreshKey="m2"/>);
    await waitFor(()=>expect(fetchMock.mock.calls.length).toBe(before+2));
    expect(screen.getByLabelText("Name")).toHaveValue("My draft name");
    expect(screen.getByLabelText("Temperament")).toHaveValue("playful");
  });

  it("uses the persisted pet name in Chat labels and updates them after a rename",async()=>{
    mockApi();render(<Chat initialMessages={[{id:"m1",role:"assistant",content:"Let us explore the garden."}]}/>);
    const response=screen.getByText("Let us explore the garden.").closest("article")!;
    expect(await screen.findByPlaceholderText("Message Pip…")).toBeInTheDocument();
    expect(within(response).getByText("Pip")).toBeInTheDocument();
    expect(within(response).queryByText("Continuity")).not.toBeInTheDocument();
    expect(screen.getByRole("heading",{name:"Chat"})).toBeInTheDocument();
    expect(screen.getByText(/Mystery Pet companion, backed by Continuity/)).toBeInTheDocument();
    await open();fireEvent.click(screen.getByText("Companion preferences"));
    fireEvent.change(screen.getByLabelText("Name"),{target:{value:"Moss"}});
    fireEvent.click(screen.getByRole("button",{name:"Save companion"}));
    expect(await screen.findByPlaceholderText("Message Moss…")).toBeInTheDocument();
    expect(within(response).getByText("Moss")).toBeInTheDocument();
  });

  it("retries a failed mount-time profile without fetching context or assistance",async()=>{
    const fetchMock=mockApi().mockResolvedValueOnce(json({},503));render(<Companion {...props}/>);
    expect(await screen.findByRole("alert")).toHaveTextContent("profile could not be loaded");
    fireEvent.click(screen.getByRole("button",{name:"Retry profile"}));
    expect(await screen.findByText("Pip")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(fetchMock.mock.calls.every(([url])=>String(url).endsWith("/api/companion"))).toBe(true);
  });

  it.each([401,503])("handles context load failure %s after loading the profile",async status=>{
    const fetchMock=mockApi();render(<Companion {...props}/>);await screen.findByText("Pip");
    fetchMock.mockResolvedValueOnce(json(profile)).mockResolvedValueOnce(json({},status));
    fireEvent.click(screen.getByRole("button",{name:"Open companion"}));
    expect(await screen.findByRole("alert")).toHaveTextContent(status===401?"session expired":"Chat is still available");
    expect(props.onSessionExpired).toHaveBeenCalledTimes(status===401?1:0);
    expect(screen.getByRole("button",{name:"Refresh companion"})).toBeEnabled();
    expect(screen.getByText("Pip")).toBeInTheDocument();
  });

  it("inspects context and drafts a contextual starting point without posting",async()=>{
    const fetchMock=mockApi();render(<Companion {...props} activeThreadId={plate.id}/>);await open();
    fireEvent.click(screen.getByText(`Inspect ${plate.title}`));
    expect(screen.getByText(plate.why)).toBeInTheDocument();
    expect(screen.getByText("Herb labels")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button",{name:"Explore alternatives"}));
    expect(props.onDraft).toHaveBeenCalledWith("Explore alternatives for Pocket garden. ");
    expect(fetchMock.mock.calls.some(([,init])=>init?.method==="POST")).toBe(false);
    fireEvent.change(screen.getByLabelText("Active project or idea"),{target:{value:""}});
    expect(props.onSelect).toHaveBeenCalledWith(null);
  });

  it("records feedback with a UUID and exposes only allowlisted diagnostics",async()=>{
    const fetchMock=mockApi();const {container}=render(<Companion {...props}/>);await open();
    fireEvent.click(screen.getByText("Assistance (1)"));fireEvent.click(screen.getByText("Diagnostics"));
    const context=within(screen.getByRole("region",{name:"Context"}));
    for(const text of ["policy_version: context-v1","included[0].id: project-1","included[0].type: thread","included[0].reason: active_context","included[0].sources[0].source_id: message-1","excluded[0].reason: char_limit","limits.max_chars: 12000","limits.recent_only: false","chars: 800","approximate_tokens: 200"])expect(context.getByText(text)).toBeInTheDocument();
    const usage=within(screen.getByRole("region",{name:"Usage"}));
    for(const text of ["[0].model: test-model","[0].input_tokens: 20","[0].output_tokens: 10","[0].latency_ms: 42","[0].cost_usd: 0.01","[1].cost_usd: not reported"])expect(usage.getByText(text)).toBeInTheDocument();
    const policy=within(screen.getByRole("region",{name:"Policy"}));
    expect(policy.getByText("reason: respond_to_request")).toBeInTheDocument();
    expect(policy.getByText("allow_memory_write: true")).toBeInTheDocument();
    const tools=within(screen.getByRole("region",{name:"Tool receipts"}));
    for(const text of ["[0].tool: create_commitment","[0].status: saved","[0].id: commitment-1","[1].status: rejected","[1].reason: permission_required"])expect(tools.getByText(text)).toBeInTheDocument();
    expect(container.textContent).not.toContain("SECRET");
    fireEvent.click(screen.getByRole("button",{name:"Helpful"}));
    expect(await screen.findByText("Feedback recorded. This is not proof of learning.")).toBeInTheDocument();
    const post=fetchMock.mock.calls.find(([,init])=>init?.method==="POST");
    expect(String(post?.[0])).toContain("/api/assistance/a1/feedback");
    expect(JSON.parse(String(post?.[1]?.body))).toEqual({verdict:"helpful",event_id:expect.stringMatching(/^[a-f0-9-]{36}$/)});
  });

  it("reuses the feedback event ID after an uncertain failure",async()=>{
    const fetchMock=mockApi();render(<Companion {...props}/>);await open();fireEvent.click(screen.getByText("Assistance (1)"));
    fetchMock.mockResolvedValueOnce(json({},503));fireEvent.click(screen.getByRole("button",{name:"Completed"}));
    expect(await screen.findByRole("alert")).toHaveTextContent("Feedback could not be confirmed");
    fireEvent.click(screen.getByRole("button",{name:"Completed"}));
    await screen.findByText("Feedback recorded. This is not proof of learning.");
    const posts=fetchMock.mock.calls.filter(([,init])=>init?.method==="POST");
    expect(posts[0][1]?.body).toEqual(posts[1][1]?.body);
  });

  it("refreshes assistance when a new response arrives",async()=>{
    const fetchMock=mockApi();const {rerender}=render(<Companion {...props} refreshKey="m1"/>);await open();
    rerender(<Companion {...props} refreshKey="m2"/>);
    await waitFor(()=>expect(fetchMock.mock.calls.filter(([url])=>String(url).includes("/api/assistance?"))).toHaveLength(2));
  });

  it("reports CAS conflicts and allows reloading before another save",async()=>{
    const fetchMock=mockApi();render(<Companion {...props}/>);await open();fireEvent.click(screen.getByText("Companion preferences"));
    fetchMock.mockResolvedValueOnce(json({},409));fireEvent.click(screen.getByRole("button",{name:"Save companion"}));
    expect(await screen.findByRole("alert")).toHaveTextContent("Preferences changed elsewhere");
    fireEvent.click(screen.getByRole("button",{name:"Refresh companion"}));
    await waitFor(()=>expect(screen.queryByRole("alert")).not.toBeInTheDocument());
  });

  it.each([401,503])("handles initial endpoint failure %s without breaking Chat",async status=>{
    vi.spyOn(globalThis,"fetch").mockImplementation(async()=>json({},status));render(<Companion {...props}/>);
    expect(await screen.findByRole("alert")).toHaveTextContent(status===401?"session expired":"Chat is still available");
    expect(props.onSessionExpired).toHaveBeenCalledTimes(status===401?1:0);
    expect(screen.getByRole("button",{name:"Retry profile"})).toBeEnabled();
    expect(screen.getByRole("img",{name:/resting in a hand/})).toBeInTheDocument();
    expect(screen.getByText(/Model mode not verified/)).toBeInTheDocument();
  });

  it.each(["preferences","feedback"])("expires the session on %s mutation 401",async operation=>{
    const fetchMock=mockApi();render(<Companion {...props}/>);await open();fetchMock.mockResolvedValueOnce(json({},401));
    if(operation==="preferences"){fireEvent.click(screen.getByText("Companion preferences"));fireEvent.click(screen.getByRole("button",{name:"Save companion"}))}
    else{fireEvent.click(screen.getByText("Assistance (1)"));fireEvent.click(screen.getByRole("button",{name:"Acknowledged"}))}
    expect(await screen.findByRole("alert")).toHaveTextContent("session expired");
    expect(props.onSessionExpired).toHaveBeenCalledOnce();
  });
});

describe("Explicit message permissions",()=>{
  it("defaults to no writes, grants one message only, and sends nullable active_thread_id",async()=>{
    const fetchMock=mockApi();render(<Chat/>);
    const checkbox=screen.getByRole("checkbox",{name:"Allow saving or updating this idea, project, or commitment"});
    expect(checkbox).not.toBeChecked();
    fireEvent.change(screen.getByLabelText("Message"),{target:{value:"Explore"}});fireEvent.click(screen.getByRole("button",{name:"Send"}));
    await waitFor(()=>expect(checkbox).toBeEnabled());
    await open();fireEvent.change(screen.getByLabelText("Active project or idea"),{target:{value:plate.id}});
    fireEvent.click(checkbox);fireEvent.change(screen.getByLabelText("Message"),{target:{value:"Save this idea"}});fireEvent.click(screen.getByRole("button",{name:"Send"}));
    await waitFor(()=>expect(checkbox).toBeEnabled());expect(checkbox).not.toBeChecked();
    fireEvent.change(screen.getByLabelText("Message"),{target:{value:"Just discuss"}});fireEvent.click(screen.getByRole("button",{name:"Send"}));
    await waitFor(()=>expect(checkbox).toBeEnabled());
    const bodies=fetchMock.mock.calls.filter(([url,init])=>String(url).endsWith("/api/chat/messages")&&init?.method==="POST").map(([,init])=>JSON.parse(String(init?.body)));
    expect(bodies).toEqual([
      expect.objectContaining({content:"Explore",allow_memory_write:false,active_thread_id:null}),
      expect.objectContaining({content:"Save this idea",allow_memory_write:true,active_thread_id:plate.id}),
      expect.objectContaining({content:"Just discuss",allow_memory_write:false,active_thread_id:plate.id}),
    ]);
  });

  it("keeps the permission and selected context bound to the same event on retry",async()=>{
    const fetchMock=mockApi();render(<Chat/>);await open();
    fireEvent.change(screen.getByLabelText("Active project or idea"),{target:{value:plate.id}});
    fireEvent.click(screen.getByRole("checkbox"));fireEvent.change(screen.getByLabelText("Message"),{target:{value:"Keep this"}});
    fetchMock.mockResolvedValueOnce(json({},503));fireEvent.click(screen.getByRole("button",{name:"Send"}));
    await screen.findByRole("button",{name:"Retry"});
    fireEvent.change(screen.getByLabelText("Active project or idea"),{target:{value:""}});
    fireEvent.click(screen.getByRole("button",{name:"Retry"}));
    await waitFor(()=>expect(screen.queryByRole("button",{name:"Retry"})).not.toBeInTheDocument());
    const posts=fetchMock.mock.calls.filter(([,init])=>init?.method==="POST");
    expect(posts).toHaveLength(2);expect(posts[0][1]?.body).toEqual(posts[1][1]?.body);
    expect(JSON.parse(String(posts[1][1]?.body))).toMatchObject({allow_memory_write:true,active_thread_id:plate.id});
  });

  it("handles a message 401 through the existing session callback",async()=>{
    const fetchMock=mockApi();const expired=vi.fn();render(<Chat onSessionExpired={expired}/>);
    await screen.findByText("Pip");fetchMock.mockResolvedValueOnce(json({},401));
    fireEvent.change(screen.getByLabelText("Message"),{target:{value:"Hello"}});fireEvent.click(screen.getByRole("button",{name:"Send"}));
    expect(await screen.findByRole("alert")).toHaveTextContent("session expired");expect(expired).toHaveBeenCalledOnce();
  });
});

it("preserves title and description editing alongside context corrections",async()=>{
  const thread:ThreadItem={...plate,importance:1,deadline:null,watch:false,created_at:record.created_at,derivation:"explicit",state:"active"};
  const fetchMock=vi.spyOn(globalThis,"fetch").mockResolvedValue(json({...thread,title:"New title",description:"New description",why:"For joy",kind:"idea",epistemic_status:"hypothesis",version:4}));
  render(<Threads items={[thread]} onSessionExpired={()=>undefined}/>);fireEvent.click(screen.getByRole("button",{name:`Edit ${thread.title}`}));
  for(const [label,value] of [["Title","New title"],["Description","New description"],["Why this matters","For joy"],["Kind","idea"],["Epistemic status","hypothesis"]])fireEvent.change(screen.getByLabelText(label),{target:{value}});
  fireEvent.click(screen.getByRole("button",{name:"Save changes"}));await screen.findByText("Thread updated");
  expect(JSON.parse(String(fetchMock.mock.calls[0][1]?.body))).toMatchObject({title:"New title",description:"New description",why:"For joy",kind:"idea",epistemic_status:"hypothesis",version:3});
});
