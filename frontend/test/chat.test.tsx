import {fireEvent,render,screen,waitFor} from "@testing-library/react";
import {afterEach,describe,expect,it,vi} from "vitest";
import Chat from "../app/chat";
import Page from "../app/page";
import {mockFetchWithProfile} from "./mock-fetch";

afterEach(()=>vi.restoreAllMocks());
const page=(items:unknown[],older_cursor:string|null=null)=>({
  items,has_more:older_cursor!==null,older_cursor,limit:100,returned_count:items.length,
});

describe("Chat",()=>{
  it("renders untrusted message as text without creating executable markup",async()=>{
    mockFetchWithProfile();
    const payload='<img src=x onerror=alert(1)>';
    render(<Chat initialMessages={[{id:"1",role:"user",content:payload}]}/>);
    expect(screen.getByText(payload)).toBeInTheDocument();
    expect(document.querySelector("img")).toBeNull();
    expect(await screen.findByText("Pip")).toBeInTheDocument();
  });

  it("shows the bounded message capacity",async()=>{
    mockFetchWithProfile();
    render(<Chat/>);
    expect(screen.getByText("0 / 4000")).toBeInTheDocument();
    expect(await screen.findByText("Pip")).toBeInTheDocument();
  });

  it("reconciles an accepted send without posting it twice",async()=>{
    const fetchMock=mockFetchWithProfile()
      .mockResolvedValueOnce(new Response("{}",{status:201,headers:{"content-type":"application/json"}}))
      .mockResolvedValueOnce(new Response(null,{status:503}))
      .mockResolvedValueOnce(new Response(JSON.stringify(page([{id:"server",role:"user",content:"hello"}])),{status:200,headers:{"content-type":"application/json"}}));
    render(<Chat/>);
    fireEvent.change(screen.getByLabelText("Message"),{target:{value:"hello"}});
    fireEvent.click(screen.getByRole("button",{name:"Send"}));
    const retry=await screen.findByRole("button",{name:"Retry"});
    await waitFor(()=>expect(retry).toBeEnabled());
    fireEvent.click(retry);
    await waitFor(()=>expect(fetchMock).toHaveBeenCalledTimes(3));
    expect(fetchMock.mock.calls.filter(([,init])=>init?.method==="POST")).toHaveLength(1);
    expect(JSON.parse(String(fetchMock.mock.calls[0][1]?.body)).event_id).toBeTruthy();
    expect(fetchMock.mock.calls[2][0]).toBe("http://localhost:8000/api/chat/messages?mode=latest&limit=100");
  });

  it("keeps the newest accepted exchange visible after more than 100 rows",async()=>{
    const initial=Array.from({length:100},(_,index)=>({id:String(index+2),role:"user" as const,content:`message ${index+2}`}));
    const reconciled=[...initial.slice(2),
      {id:"102",role:"user" as const,content:"new accepted message"},
      {id:"103",role:"assistant" as const,content:"new assistant reply"},
    ];
    mockFetchWithProfile()
      .mockResolvedValueOnce(new Response("{}",{status:201,headers:{"content-type":"application/json"}}))
      .mockResolvedValueOnce(new Response(JSON.stringify(page(reconciled,"older")),{status:200,headers:{"content-type":"application/json"}}));
    const {container}=render(<Chat initialMessages={initial} initialOlderCursor="older" initialHasMore/>);
    fireEvent.change(screen.getByLabelText("Message"),{target:{value:"new accepted message"}});
    fireEvent.click(screen.getByRole("button",{name:"Send"}));

    expect(await screen.findByText("new assistant reply")).toBeInTheDocument();
    expect(screen.getAllByText("new accepted message")).toHaveLength(1);
    expect(container.querySelectorAll("article.message")).toHaveLength(102);
    expect([...container.querySelectorAll("article.message p")].at(-1)).toHaveTextContent("new assistant reply");
  });

  it("prepends earlier messages once and keeps the load control focused",async()=>{
    const latest=Array.from({length:100},(_,index)=>({id:String(index+2),role:"user" as const,content:`message ${index+2}`}));
    const fetchMock=mockFetchWithProfile().mockResolvedValueOnce(
      new Response(JSON.stringify(page([
        {id:"0",role:"user",content:"message 0"},
        {id:"1",role:"assistant",content:"message 1"},
      ],"oldest-page")),{status:200,headers:{"content-type":"application/json"}}),
    );
    const {container}=render(<Chat initialMessages={latest} initialOlderCursor="older-page" initialHasMore/>);
    const load=screen.getByRole("button",{name:"Load earlier messages"});
    load.focus();
    fireEvent.click(load);

    expect(await screen.findByText("message 0")).toBeInTheDocument();
    expect(fetchMock.mock.calls[0][0]).toBe("http://localhost:8000/api/chat/messages?mode=latest&limit=100&cursor=older-page");
    expect(container.querySelectorAll("article.message")).toHaveLength(102);
    expect([...container.querySelectorAll("article.message p")].map(node=>node.textContent)).toEqual(
      Array.from({length:102},(_,index)=>`message ${index}`),
    );
    expect(load).toHaveFocus();
  });

  it("moves focus to the oldest loaded message when the final page removes the load control",async()=>{
    const latest=Array.from({length:100},(_,index)=>({id:String(index+2),role:"user" as const,content:`message ${index+2}`}));
    mockFetchWithProfile().mockResolvedValueOnce(
      new Response(JSON.stringify(page([
        {id:"0",role:"user",content:"message 0"},
        {id:"1",role:"assistant",content:"message 1"},
      ])),{status:200,headers:{"content-type":"application/json"}}),
    );
    const {container}=render(<Chat initialMessages={latest} initialOlderCursor="final-page" initialHasMore/>);
    const viewport=container.querySelector<HTMLElement>(".messages");
    if(!viewport)throw new Error("Expected chat message viewport");
    let heightRead=0;
    Object.defineProperty(viewport,"scrollHeight",{configurable:true,get:()=>heightRead++===0?500:700});
    viewport.scrollTop=37;
    const load=screen.getByRole("button",{name:"Load earlier messages"});
    expect(load).toBeInstanceOf(HTMLButtonElement);
    expect(load).toHaveAttribute("type","button");
    load.focus();
    load.click();

    const oldestMessage=(await screen.findByText("message 0")).closest("article");
    expect(screen.queryByRole("button",{name:"Load earlier messages"})).not.toBeInTheDocument();
    expect(oldestMessage).toHaveAttribute("tabindex","-1");
    await waitFor(()=>expect(oldestMessage).toHaveFocus());
    expect(viewport.scrollTop).toBe(237);
    expect(screen.getByRole("status")).toHaveTextContent("Earlier messages loaded. Start of conversation reached.");
    expect([...container.querySelectorAll("article.message p")].map(node=>node.textContent)).toEqual(
      Array.from({length:102},(_,index)=>`message ${index}`),
    );
  });
});

describe("Page",()=>{
  it("fetches and renders prior messages immediately after login",async()=>{
    const prior=[
      {id:"1",role:"user",content:"Earlier question"},
      {id:"2",role:"assistant",content:"Earlier answer"},
    ];
    const fetchMock=mockFetchWithProfile()
      .mockResolvedValueOnce(new Response(null,{status:401}))
      .mockResolvedValueOnce(new Response(null,{status:200}))
      .mockResolvedValueOnce(new Response(JSON.stringify(page(prior)),{
        status:200,
        headers:{"content-type":"application/json"},
      }))
      .mockResolvedValueOnce(new Response(JSON.stringify({
        local_date:"2026-09-30",briefing:null,vocabulary:null,power_list:[],open_commitments:[],
        resurfaced_items:[],evening_reflection:{status:"not_started",journal:null},
      }),{status:200,headers:{"content-type":"application/json"}}))
      .mockResolvedValueOnce(new Response(JSON.stringify({items:[],has_more:false}),{
        status:200,headers:{"content-type":"application/json"},
      }))
      .mockResolvedValueOnce(new Response(JSON.stringify({timezone:"UTC",morning_time:"08:00",evening_time:"20:00",onboarding_complete:true,version:1}),{status:200,headers:{"content-type":"application/json"}}));

    render(<Page/>);
    await screen.findByLabelText("Access code");
    fireEvent.change(screen.getByLabelText("Access code"),{target:{value:"secret"}});
    fireEvent.click(screen.getByRole("button",{name:"Sign in"}));

    expect(await screen.findByText("Earlier question")).toBeInTheDocument();
    expect(screen.getByText("Earlier answer")).toBeInTheDocument();
    await waitFor(()=>expect(fetchMock.mock.calls.length).toBeGreaterThanOrEqual(5));
    expect(fetchMock.mock.calls[2][0]).toBe("http://localhost:8000/api/chat/messages?mode=latest&limit=100");
    expect(fetchMock.mock.calls[3][0]).toBe("http://localhost:8000/api/today");
    expect(fetchMock.mock.calls[4][0]).toBe("http://localhost:8000/api/vocabulary?limit=20&offset=0");
  });

  it("renders Today data for an authenticated user",async()=>{
    const today={
      local_date:"2026-09-30",
      briefing:{content:"Morning continuity",local_date:"2026-09-30"},
      vocabulary:null,
      power_list:[],
      open_commitments:[],
      resurfaced_items:[],
      evening_reflection:{status:"not_started",journal:null},
    };
    const fetchMock=mockFetchWithProfile()
      .mockResolvedValueOnce(new Response("[]",{status:200,headers:{"content-type":"application/json"}}))
      .mockResolvedValueOnce(new Response(JSON.stringify(today),{status:200,headers:{"content-type":"application/json"}}))
      .mockResolvedValueOnce(new Response(JSON.stringify({items:[],has_more:false}),{status:200,headers:{"content-type":"application/json"}}));
    fetchMock.mockResolvedValueOnce(new Response(JSON.stringify({timezone:"UTC",morning_time:"08:00",evening_time:"20:00",onboarding_complete:true,version:1}),{status:200,headers:{"content-type":"application/json"}}));

    render(<Page/>);

    const todayButton=await screen.findByRole("button",{name:"Today"});
    fireEvent.click(todayButton);
    expect(await screen.findByRole("heading",{name:"Today"})).toBeInTheDocument();
    expect(screen.getByText("Morning continuity")).toBeInTheDocument();
  });
});
