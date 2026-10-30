import {afterEach,expect,it,vi} from "vitest";
import {cleanup,fireEvent,render,screen,waitFor} from "@testing-library/react";
import Autonomy from "../app/autonomy";

afterEach(()=>{cleanup();vi.restoreAllMocks();});
const json=(body:unknown,status=200)=>new Response(JSON.stringify(body),{status,headers:{"content-type":"application/json"}});
const preferences={version:1,timezone:"Africa/Lagos",checkins_enabled:false,morning_enabled:false,evening_enabled:false,morning_time:"08:00",evening_time:"20:00",quiet_start:21,quiet_end:8,worker:"not_started",last_worker_at:null};
const commitment={id:"presentation",version:1,title:"Finish presentation",state:"open",purpose:"Explain research",next_action:"Make outline",due_local:"2026-10-27T21:00:00+01:00",due_bound:"exclusive",next_check_at:null,ambiguity:"none",followup_enabled:true,followup_minutes:1440,followup_count:0,max_followups:3};

it("shows opt-in defaults and submits only explicitly enabled routines",async()=>{
  const writes:RequestInit[]=[];
  const fetch=vi.spyOn(globalThis,"fetch").mockImplementation(async(input,init)=>{
    if(init?.method==="PATCH"){writes.push(init);return json(preferences);}
    return String(input).includes("/actions")?json({items:[],has_more:false}):json({preferences,commitments:[commitment],has_more:false});
  });
  render(<Autonomy onExpired={()=>undefined} onRefresh={async()=>undefined}/>);
  expect(fetch).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button",{name:"Open autonomy controls"}));
  expect(await screen.findByText(/Worker: not started/)).toBeInTheDocument();
  fireEvent.click(screen.getByText("Check-ins, routines and quiet hours"));
  const enable=screen.getByLabelText("Enable daily morning briefing");
  expect(enable).not.toBeChecked();
  fireEvent.click(enable);
  fireEvent.click(screen.getByRole("button",{name:"Save autonomy preferences"}));
  await screen.findByRole("status");
  expect(writes).toHaveLength(1);
  expect(JSON.parse(String(writes[0].body))).toMatchObject({version:1,morning_enabled:true,evening_enabled:false,checkins_enabled:false});
});

it("preserves the action ID on uncertain retry and displays persisted pause state",async()=>{
  const writes:Record<string,unknown>[]=[];
  let current=commitment;
  vi.spyOn(globalThis,"fetch").mockImplementation(async(input,init)=>{
    if(init?.method==="POST"){
      writes.push(JSON.parse(String(init.body)));
      if(writes.length===1)throw new Error("connection lost");
      current={...commitment,state:"paused",version:2};return json(current);
    }
    return String(input).includes("/actions")?json({items:[],has_more:false}):json({preferences,commitments:[current],has_more:false});
  });
  render(<Autonomy onExpired={()=>undefined} onRefresh={async()=>undefined}/>);
  fireEvent.click(screen.getByRole("button",{name:"Open autonomy controls"}));
  fireEvent.click(await screen.findByRole("button",{name:"Pause"}));
  await screen.findByRole("alert");
  fireEvent.click(screen.getByRole("button",{name:"Pause"}));
  await screen.findByRole("button",{name:"Resume"});
  expect(writes).toHaveLength(2);
  expect(writes[0]).toEqual(writes[1]);
  expect(writes[0]).toMatchObject({action:"pause",version:1,id:"presentation"});
  expect(writes[0].event_id).toBeTruthy();
});

it("shows verified in-app evidence and handles expired sessions",async()=>{
  const expired=vi.fn();
  const fetch=vi.spyOn(globalThis,"fetch").mockImplementation(async input=>String(input).includes("/actions")?json({items:[{
    id:"action-1",kind:"commitment_followup",state:"succeeded",reason:"checkin_saved_in_app",
    due_at:"2026-10-27T09:00:00Z",attempt_count:1,message_id:"message-1",source_event_id:"event-1",decision:{delivery:"in_app"},
  }],has_more:false}):json({preferences,commitments:[],has_more:false}));
  render(<Autonomy onExpired={expired} onRefresh={async()=>undefined}/>);
  fireEvent.click(screen.getByRole("button",{name:"Open autonomy controls"}));
  fireEvent.click(await screen.findByText("Execution history (1)"));
  expect(screen.getByText(/delivery: in_app/)).toBeInTheDocument();
  expect(screen.getByText("message-1")).toBeInTheDocument();
  fetch.mockImplementation(async()=>json({detail:"Authentication required"},401));
  fireEvent.click(screen.getByRole("button",{name:"Refresh autonomy"}));
  await waitFor(()=>expect(expired).toHaveBeenCalled());
});
