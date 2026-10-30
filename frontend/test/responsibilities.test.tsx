import {afterEach,describe,expect,it,vi} from "vitest";
import {cleanup,fireEvent,render,screen,waitFor,within} from "@testing-library/react";
import Responsibilities from "../app/responsibilities";

afterEach(()=>{cleanup();vi.restoreAllMocks()});
const json=(value:unknown,status=200)=>new Response(JSON.stringify(value),{status,headers:{"content-type":"application/json"}});
const channel={account:{id:"channel",address:"+15551230000",assistant_name:"Mica",on_behalf_of:"Morgan",enabled:true,quiet_start:21,quiet_end:8},connection:{status:"not_configured"}};
const draft={id:"responsibility",scope_hash:"reviewed-scope-hash",title:"Send the document",due_at:"2026-12-01T12:00:00Z",state:"draft",recipient_name:"Alex",recipient_address:"+15551230001",expected_evidence:"Document link",followup_minutes:1440,max_followups:2,followup_count:0,attention_reason:null,version:1,initial_message:"I'm Mica, an AI assistant acting on behalf of Morgan.\nPlease send the document.",followup_message:"Following up on the document.",last_report:"",reported_complete_at:null,verified_at:null,verification_note:"",agreement_at:null,messages:[],events:[]};

describe("Operational responsibilities",()=>{
  it("loads only when opened and displays an honest unconfigured channel",async()=>{
    const fetch=vi.spyOn(globalThis,"fetch").mockImplementation(async input=>String(input).endsWith("/channel")?json(channel):json({items:[],has_more:false}));
    render(<Responsibilities onExpired={()=>undefined}/>);
    expect(fetch).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button",{name:"Open responsibilities"}));
    expect(await screen.findByText(/WhatsApp connection · not_configured/)).toBeInTheDocument();
    expect(screen.getByText(/Configure CHANNEL_ACCOUNT_ID/)).toBeInTheDocument();
    expect(screen.queryByRole("img")).not.toBeInTheDocument();
  });

  it("requires explicit approval and sends the exact reviewed scope hash",async()=>{
    const calls:RequestInit[]=[];
    vi.spyOn(globalThis,"fetch").mockImplementation(async(input,init)=>{
      const url=String(input);
      if(init?.method==="POST"){calls.push(init);return json({...draft,state:"active",version:2})}
      if(url.endsWith("/channel"))return json(channel);
      if(url.endsWith("/responsibility"))return json(draft);
      return json({items:[draft],has_more:false});
    });
    render(<Responsibilities onExpired={()=>undefined}/>);
    fireEvent.click(screen.getByRole("button",{name:"Open responsibilities"}));
    fireEvent.click(await screen.findByRole("button",{name:"Review responsibility"}));
    const dialog=await screen.findByRole("dialog",{name:"Review responsibility"});
    expect(within(dialog).getByRole("button",{name:"Authorize contact"})).toBeDisabled();
    expect(within(dialog).getByText(draft.initial_message.replace(/\s+/g," "))).toBeInTheDocument();
    fireEvent.click(within(dialog).getByRole("checkbox"));
    fireEvent.click(within(dialog).getByRole("button",{name:"Authorize contact"}));
    await waitFor(()=>expect(calls).toHaveLength(1));
    expect(JSON.parse(String(calls[0].body))).toMatchObject({action:"authorize",scope_hash:"reviewed-scope-hash",version:1,approve_external_contact:true});
  });

  it("does not equate recipient-reported completion with verification",async()=>{
    const reported={...draft,state:"completion_reported",reported_complete_at:"2026-10-22T12:00:00Z",last_report:"DONE: document link",attention_reason:"completion_needs_verification"};
    const posted:RequestInit[]=[];
    vi.spyOn(globalThis,"fetch").mockImplementation(async(input,init)=>{
      if(init?.method==="POST"){posted.push(init);return json({...reported,state:"verified",verified_at:"2026-10-22T13:00:00Z",verification_note:"I reviewed it"})}
      if(String(input).endsWith("/channel"))return json(channel);
      if(String(input).endsWith("/responsibility"))return json(reported);
      return json({items:[reported]});
    });
    render(<Responsibilities onExpired={()=>undefined}/>);
    fireEvent.click(screen.getByRole("button",{name:"Open responsibilities"}));
    expect(await screen.findByText(/Completion reported, not verified/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button",{name:"Review responsibility"}));
    const button=await screen.findByRole("button",{name:"Confirm verified completion"});
    expect(button).toBeDisabled();
    fireEvent.change(screen.getByLabelText("Evidence you reviewed"),{target:{value:"I reviewed it"}});
    fireEvent.click(button);
    await waitFor(()=>expect(posted).toHaveLength(1));
    expect(JSON.parse(String(posted[0].body))).toMatchObject({action:"verify",note:"I reviewed it"});
  });

  it("shows the private pairing QR only when supplied and handles session expiry",async()=>{
    const expired=vi.fn();
    vi.spyOn(globalThis,"fetch").mockImplementation(async(input,init)=>{
      if(init?.method==="POST")return json({detail:"Authentication required"},401);
      return String(input).endsWith("/channel")?json({...channel,connection:{status:"awaiting_pairing",qr_image:"data:image/png;base64,c3ludGhldGlj"}}):json({items:[]});
    });
    render(<Responsibilities onExpired={expired}/>);
    fireEvent.click(screen.getByRole("button",{name:"Open responsibilities"}));
    expect(await screen.findByRole("img",{name:"Pair the dedicated assistant WhatsApp account"})).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button",{name:"Pair / reconnect"}));
    await waitFor(()=>expect(expired).toHaveBeenCalled());
  });
});
