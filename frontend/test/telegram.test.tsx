import {afterEach,expect,it,vi} from "vitest";
import {cleanup,fireEvent,render,screen,waitFor} from "@testing-library/react";
import Telegram from "../app/telegram";

afterEach(()=>{cleanup();vi.restoreAllMocks();});
const json=(body:unknown)=>new Response(JSON.stringify(body),{headers:{"content-type":"application/json"}});
const status={configured:true,bot_username:"MicaTestBot",worker:"recent_heartbeat",linked:false,allow_memory_write:false,proactive_enabled:false,deliveries:[]};

it("links only with explicit permissions and presents a one-use invitation",async()=>{
  const posted:RequestInit[]=[];
  const fetch=vi.spyOn(globalThis,"fetch").mockImplementation(async(_,init)=>{
    if(init?.method==="POST"){posted.push(init);return json({url:"https://t.me/MicaTestBot?start=private-code",expires_at:"2026-12-01T12:00:00Z"});}
    return json(status);
  });
  render(<Telegram onExpired={()=>undefined}/>);
  expect(fetch).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button",{name:"Open Telegram"}));
  const write=await screen.findByLabelText(/Allow my linked DMs/);
  expect(write).not.toBeChecked();
  fireEvent.click(write);
  fireEvent.click(screen.getByLabelText(/Deliver my enabled check-ins/));
  fireEvent.click(screen.getByRole("button",{name:"Create private linking invitation"}));
  expect(await screen.findByRole("link",{name:"Open your bot and press Start"})).toHaveAttribute("rel","noreferrer");
  expect(JSON.parse(String(posted[0].body))).toEqual({allow_memory_write:true,proactive_enabled:true});
});

it("shows uncertainty honestly and supports connection revocation",async()=>{
  const methods:string[]=[];
  let linked=true;
  vi.spyOn(globalThis,"fetch").mockImplementation(async(_,init)=>{
    if(init?.method){methods.push(init.method);linked=false;return json({disconnected:true});}
    return json({...status,linked,deliveries:[{id:"delivery",state:"uncertain",reason:"interrupted_send",attempts:1}]});
  });
  render(<Telegram onExpired={()=>undefined}/>);
  fireEvent.click(screen.getByRole("button",{name:"Open Telegram"}));
  fireEvent.click(await screen.findByText("Telegram delivery history"));
  expect(screen.getByText(/will not be resent automatically/)).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button",{name:"Disconnect Telegram"}));
  await waitFor(()=>expect(methods).toEqual(["DELETE"]));
  expect(await screen.findByRole("button",{name:"Create private linking invitation"})).toBeInTheDocument();
});
