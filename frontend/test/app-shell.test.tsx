import {act,fireEvent,render,screen,waitFor,within} from "@testing-library/react";
import {afterEach,describe,expect,it,vi} from "vitest";
import Page from "../app/page";
import Threads,{type ThreadItem} from "../app/threads";
import History,{type TimelineItem} from "../app/history";
import {mockFetchWithProfile} from "./mock-fetch";

const emptyToday={local_date:"2026-10-01",briefing:null,vocabulary:null,power_list:[],open_commitments:[],active_threads:[],resurfaced_items:[],evening_reflection:{status:"not_started",journal:null}};
const emptyChatPage={items:[],has_more:false,older_cursor:null,limit:100,returned_count:0};
const prefs={timezone:"UTC",morning_time:"08:00",evening_time:"20:00",onboarding_complete:true,version:1};
const recoveredThread:ThreadItem={id:"recovered",title:"Recovered thread",description:"Loaded after retry",state:"active",importance:1,deadline:null,next_action:"Continue",watch:false,version:1,created_at:"2026-10-01T08:00:00Z",derivation:"explicit"};
function json(value:unknown,status=200){return new Response(JSON.stringify(value),{status,headers:{"content-type":"application/json"}})}
function deferred<T>(){let resolve!:(value:T)=>void;const promise=new Promise<T>(done=>{resolve=done});return {promise,resolve}}

afterEach(()=>vi.restoreAllMocks());

describe("Milestone 5 shell",()=>{
  it("explains selective memory and privacy before access-code login",async()=>{
    vi.spyOn(globalThis,"fetch").mockResolvedValue(new Response(null,{status:401}));
    render(<Page/>);
    expect(await screen.findByRole("heading",{name:"Your days, carried forward"})).toBeInTheDocument();
    expect(screen.getByText(/selective memory/i)).toBeInTheDocument();
    expect(screen.getByText(/inspect, correct, or delete/i)).toBeInTheDocument();
    expect(screen.getByText(/retention/i)).toBeInTheDocument();
    expect(screen.getByLabelText("Access code")).toHaveAttribute("autocomplete","current-password");
  });

  it("loads history after login and navigates among only four product areas",async()=>{
    const fetchMock=mockFetchWithProfile()
      .mockResolvedValueOnce(new Response(null,{status:401}))
      .mockResolvedValueOnce(json({username:"owner"}))
      .mockResolvedValueOnce(json(emptyChatPage))
      .mockResolvedValueOnce(json(emptyToday))
      .mockResolvedValueOnce(json({items:[],has_more:false}))
      .mockResolvedValueOnce(json(prefs))
      .mockResolvedValueOnce(json([]))
      .mockImplementation(async()=>json({items:[{id:"e1",kind:"episode",content:"Launch mapped",actor:"joint",created_at:"2026-10-01T08:00:00Z",version:1,editable:true,provenance_target_id:"e1"}],has_more:false}));
    render(<Page/>);
    fireEvent.change(await screen.findByLabelText("Access code"),{target:{value:"secret"}});
    fireEvent.click(screen.getByRole("button",{name:"Sign in"}));
    const nav=await screen.findByRole("navigation",{name:"Primary"});
    const buttons=within(nav).getAllByRole("button");
    expect(buttons).toHaveLength(4);
    for(const label of ["Chat","Today","Threads","History"]) expect(within(nav).getByRole("button",{name:label})).toBeInTheDocument();
    fireEvent.click(within(nav).getByRole("button",{name:"History"}));
    expect(await screen.findByText("Launch mapped")).toBeInTheDocument();
    expect(fetchMock.mock.calls.some(call=>String(call[0]).includes("/api/history/timeline"))).toBe(true);
  });

  it("shows a retryable load error and session-expiry message",async()=>{
    mockFetchWithProfile()
      .mockResolvedValueOnce(json(emptyChatPage))
      .mockResolvedValueOnce(new Response(null,{status:503}))
      .mockResolvedValueOnce(json(emptyToday))
      .mockResolvedValueOnce(json({items:[],has_more:false}));
    render(<Page/>);
    expect(await screen.findByRole("alert")).toHaveTextContent(/could not load/i);
    expect(screen.getByRole("button",{name:"Try again"})).toBeInTheDocument();
  });

  it("keeps Threads and History in per-view loading states until their requests settle",async()=>{
    const threadsRequest=deferred<Response>();
    const historyRequest=deferred<Response>();
    mockFetchWithProfile()
      .mockResolvedValueOnce(json(emptyChatPage))
      .mockResolvedValueOnce(json(emptyToday))
      .mockResolvedValueOnce(json({items:[],has_more:false}))
      .mockResolvedValueOnce(json(prefs))
      .mockResolvedValueOnce(json([]))
      .mockResolvedValueOnce(json({items:[],has_more:false}))
      .mockImplementationOnce(()=>threadsRequest.promise)
      .mockImplementationOnce(()=>historyRequest.promise);
    render(<Page/>);
    const nav=await screen.findByRole("navigation",{name:"Primary"});
    fireEvent.click(within(nav).getByRole("button",{name:"Threads"}));
    expect(screen.getByRole("status")).toHaveTextContent("Loading threads");
    expect(screen.queryByText("Nothing here yet")).not.toBeInTheDocument();
    threadsRequest.resolve(json([]));
    expect(await screen.findByText("Nothing here yet")).toBeInTheDocument();
    fireEvent.click(within(nav).getByRole("button",{name:"History"}));
    expect(screen.getByRole("status")).toHaveTextContent("Loading history");
    expect(screen.queryByText("No memories found")).not.toBeInTheDocument();
    historyRequest.resolve(json({items:[],has_more:false}));
    expect(await screen.findByText("No memories found")).toBeInTheDocument();
  });

  it("keeps failed empty views distinct and retries only their own endpoint",async()=>{
    const fetchMock=mockFetchWithProfile()
      .mockResolvedValueOnce(json(emptyChatPage))
      .mockResolvedValueOnce(json(emptyToday))
      .mockResolvedValueOnce(json({items:[],has_more:false}))
      .mockResolvedValueOnce(json(prefs))
      .mockResolvedValueOnce(new Response(null,{status:503}))
      .mockResolvedValueOnce(new Response(null,{status:503}))
      .mockResolvedValueOnce(new Response(null,{status:503}))
      .mockResolvedValueOnce(json([recoveredThread]))
      .mockResolvedValueOnce(new Response(null,{status:503}))
      .mockResolvedValueOnce(json({items:[],has_more:false}));
    render(<Page/>);
    const nav=await screen.findByRole("navigation",{name:"Primary"});
    fireEvent.click(within(nav).getByRole("button",{name:"Threads"}));
    expect(await screen.findByRole("alert")).toHaveTextContent("Threads could not be refreshed");
    expect(screen.queryByText("Nothing here yet")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button",{name:"Try again"}));
    expect(await screen.findByText("Recovered thread")).toBeInTheDocument();
    expect(String(fetchMock.mock.calls.at(-1)?.[0])).toContain("/api/threads?limit=100");

    fireEvent.click(within(nav).getByRole("button",{name:"History"}));
    expect(await screen.findByRole("alert")).toHaveTextContent("History could not be refreshed");
    expect(screen.queryByText("No memories found")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button",{name:"Try again"}));
    expect(await screen.findByText("No memories found")).toBeInTheDocument();
    expect(String(fetchMock.mock.calls.at(-1)?.[0])).toContain("/api/history/timeline");
  });

  it("reloads Threads and History on entry after a Chat save without reloading the page",async()=>{
    let saved=false;
    const historyItem={id:"e1",kind:"episode",content:"Garden plan retained",actor:"user",created_at:"2026-10-01T08:00:00Z",version:1,editable:true,provenance_target_id:"e1"};
    const fetchMock=mockFetchWithProfile().mockImplementation(async(input,init)=>{
      const path=new URL(String(input)).pathname;
      if(path==="/api/chat/messages"){
        if(init?.method==="POST"){saved=true;return json({})}
        return json({...emptyChatPage,items:saved?[{id:"m1",role:"assistant",content:"Saved the garden plan."}]:[]});
      }
      if(path==="/api/today")return json(emptyToday);
      if(path==="/api/preferences")return json(prefs);
      if(path==="/api/vocabulary")return json({items:[]});
      if(path==="/api/threads")return json(saved?[{...recoveredThread,title:"Garden project"}]:[]);
      if(path==="/api/history/timeline")return json({items:saved?[historyItem]:[],has_more:false});
      throw new Error(`Unexpected request: ${path}`);
    });
    render(<Page/>);const nav=await screen.findByRole("navigation",{name:"Primary"});
    fireEvent.change(screen.getByLabelText("Message"),{target:{value:"Save the garden plan"}});
    fireEvent.click(screen.getByRole("checkbox",{name:"Allow saving or updating this idea, project, or commitment"}));
    fireEvent.click(screen.getByRole("button",{name:"Send"}));await screen.findByText("Saved the garden plan.");
    fireEvent.click(within(nav).getByRole("button",{name:"Threads"}));
    expect(await screen.findByText("Garden project")).toBeInTheDocument();
    expect(fetchMock.mock.calls.filter(([url])=>String(url).includes("/api/threads?"))).toHaveLength(2);
    fireEvent.click(within(nav).getByRole("button",{name:"History"}));
    expect(await screen.findByText("Garden plan retained")).toBeInTheDocument();
    expect(fetchMock.mock.calls.filter(([url])=>String(url).includes("/api/history/timeline?"))).toHaveLength(2);
    fireEvent.click(within(nav).getByRole("button",{name:"Chat"}));
    fireEvent.click(within(nav).getByRole("button",{name:"Threads"}));
    expect(await screen.findByText("Garden project")).toBeInTheDocument();
    expect(fetchMock.mock.calls.filter(([url])=>String(url).includes("/api/threads?"))).toHaveLength(3);
  });

  it("does not let an older background Threads request clear navigation loading or replace fresh data",async()=>{
    const initial=deferred<Response>();const current=deferred<Response>();
    mockFetchWithProfile()
      .mockResolvedValueOnce(json(emptyChatPage)).mockResolvedValueOnce(json(emptyToday))
      .mockResolvedValueOnce(json({items:[]})).mockResolvedValueOnce(json(prefs))
      .mockImplementationOnce(()=>initial.promise).mockResolvedValueOnce(json({items:[],has_more:false}))
      .mockImplementationOnce(()=>current.promise);
    render(<Page/>);const nav=await screen.findByRole("navigation",{name:"Primary"});
    fireEvent.click(within(nav).getByRole("button",{name:"Threads"}));
    await act(async()=>{initial.resolve(json([]))});
    expect(screen.getByRole("status")).toHaveTextContent("Loading threads");
    expect(screen.queryByText("Nothing here yet")).not.toBeInTheDocument();
    current.resolve(json([recoveredThread]));
    expect(await screen.findByText("Recovered thread")).toBeInTheDocument();
  });

  it.each(["Threads","History"])("expires the session when entering %s returns 401",async view=>{
    mockFetchWithProfile()
      .mockResolvedValueOnce(json(emptyChatPage)).mockResolvedValueOnce(json(emptyToday))
      .mockResolvedValueOnce(json({items:[]})).mockResolvedValueOnce(json(prefs))
      .mockResolvedValueOnce(json([])).mockResolvedValueOnce(json({items:[],has_more:false}))
      .mockResolvedValueOnce(json({},401));
    render(<Page/>);const nav=await screen.findByRole("navigation",{name:"Primary"});
    fireEvent.click(within(nav).getByRole("button",{name:view}));
    expect(await screen.findByLabelText("Access code")).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("Your session expired");
  });
});

describe("Threads correction",()=>{
  const item:ThreadItem={id:"t1",title:"Model lab",description:"Explore",state:"active",importance:2,deadline:null,next_action:"Test",watch:false,version:1,created_at:"2026-10-01T08:00:00Z",derivation:"explicit"};
  it("filters groups, validates edits, and reports stale conflicts",async()=>{
    vi.spyOn(globalThis,"fetch").mockResolvedValue(json({detail:"Version conflict"},409));
    render(<Threads items={[item]} onSessionExpired={()=>undefined}/>);
    fireEvent.click(screen.getByRole("button",{name:"Edit Model lab"}));
    fireEvent.change(screen.getByLabelText("Title"),{target:{value:"   "}});
    fireEvent.click(screen.getByRole("button",{name:"Save changes"}));
    expect(screen.getByRole("alert")).toHaveTextContent("Title is required");
    fireEvent.change(screen.getByLabelText("Title"),{target:{value:"Model lab revised"}});
    fireEvent.click(screen.getByRole("button",{name:"Save changes"}));
    expect(await screen.findByText(/changed somewhere else/i)).toBeInTheDocument();
  });
});

describe("History correction",()=>{
  const item:TimelineItem={id:"e1",kind:"episode",content:"Shared launch",actor:"joint",created_at:"2026-10-01T08:00:00Z",version:1,editable:true,provenance_target_id:"e1"};
  it("edits a retained episode with CAS",async()=>{
    const onReload=vi.fn();
    vi.spyOn(globalThis,"fetch")
      .mockResolvedValueOnce(json({...item,content:"Corrected launch",summary:"Shared launch",version:1}))
      .mockResolvedValueOnce(json({...item,content:"Corrected launch",summary:"Corrected launch",version:2}));
    render(<History items={[item]} onReload={onReload} onSessionExpired={()=>undefined}/>);
    fireEvent.click(screen.getByRole("button",{name:"Edit Shared launch"}));
    fireEvent.change(screen.getByLabelText("Memory text"),{target:{value:" Corrected launch "}});
    fireEvent.click(screen.getByRole("button",{name:"Save correction"}));
    await waitFor(()=>expect(onReload).toHaveBeenCalled());
    expect(globalThis.fetch).toHaveBeenCalledWith(expect.stringContaining("/api/episodes/e1"),expect.objectContaining({method:"PATCH"}));
  });

  it("searches and requires confirmation before delete",async()=>{
    const onReload=vi.fn();
    vi.spyOn(globalThis,"fetch").mockResolvedValue(new Response(null,{status:204}));
    render(<History items={[item]} onReload={onReload} onSessionExpired={()=>undefined}/>);
    expect(screen.getByText("Joint")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button",{name:"Delete Shared launch"}));
    expect(screen.getByRole("dialog",{name:"Delete memory?"})).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button",{name:"Delete permanently"}));
    await waitFor(()=>expect(onReload).toHaveBeenCalled());
  });

  it("expires the session when provenance inspection returns 401",async()=>{
    const onExpired=vi.fn();
    vi.spyOn(globalThis,"fetch").mockResolvedValue(new Response(null,{status:401}));
    render(<History items={[item]} onReload={()=>undefined} onSessionExpired={onExpired}/>);
    fireEvent.click(screen.getByRole("button",{name:"View source"}));
    await waitFor(()=>expect(onExpired).toHaveBeenCalledOnce());
    expect(screen.queryByText("No additional provenance is available.")).not.toBeInTheDocument();
  });

  it("shows a retryable provenance error instead of claiming provenance is absent",async()=>{
    vi.spyOn(globalThis,"fetch").mockResolvedValue(new Response(null,{status:503}));
    render(<History items={[item]} onReload={()=>undefined} onSessionExpired={()=>undefined}/>);
    fireEvent.click(screen.getByRole("button",{name:"View source"}));
    expect(await screen.findByRole("alert")).toHaveTextContent("Could not load provenance");
    expect(screen.getByRole("button",{name:"Retry source lookup"})).toBeInTheDocument();
  });
});
