import {afterEach,expect,it,vi} from "vitest";
import {cleanup,fireEvent,render,screen,waitFor} from "@testing-library/react";
import Continuations from "../app/continuations";
afterEach(()=>{cleanup();vi.restoreAllMocks();});
const json=(body:unknown)=>new Response(JSON.stringify(body),{headers:{"content-type":"application/json"}});

it("distinguishes one-time reminders from proposals and applies versioned controls",async()=>{
  let state="active";const writes:Record<string,unknown>[]=[];
  const fetch=vi.spyOn(globalThis,"fetch").mockImplementation(async(_,init)=>{
    if(init?.method==="POST"){writes.push(JSON.parse(String(init.body)));state="paused";return json({status:"updated"});}
    return json({reminders:[{id:"r1",version:state==="active"?1:2,topic:"Find notes",mode:"once",state,next_due_at:state==="active"?"2026-10-29T15:00:00Z":null,timezone:"UTC",interval_seconds:null,occurrences:0,max_occurrences:1}],proposals:[{id:"p1",title:"Focus alternative",version:1,state:"proposed",arguments:{interval_seconds:480}}],workflows:[],scheduling_policy:{minimum_once_seconds:1}});
  });
  render(<Continuations onExpired={()=>undefined}/>);
  expect(fetch).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button",{name:"Open reminders"}));
  expect(await screen.findByText("One-time reminder · active")).toBeInTheDocument();
  expect(screen.getByText("Focus alternative")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button",{name:"Pause reminder"}));
  await waitFor(()=>expect(writes).toHaveLength(1));
  expect(writes[0]).toMatchObject({id:"r1",version:1,action:"pause"});
  expect(writes[0].event_id).toBeTruthy();
  expect(await screen.findByRole("button",{name:"Resume reminder"})).toBeInTheDocument();
});
