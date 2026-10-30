import {render,screen} from "@testing-library/react";
import {describe,expect,it} from "vitest";
import Today,{type TodayData} from "../app/today";

const data:TodayData={
  local_date:"2026-09-30",
  briefing:{content:"Good morning.",local_date:"2026-09-30"},
  vocabulary:{word:"inchoate",definition:"only partly in existence; imperfectly formed",example:"The proposal was still inchoate.",mode:"new",reason:"no weak word is due",mastery_state:"new",next_review_at:"2026-10-01T08:00:00Z"},
  power_list:[{id:"1",title:"Submit demo",reason:"deadline: Friday",kind:"commitment",score:40}],
  open_commitments:[{
    id:"1",title:"Submit demo",deadline_hint:"Friday",
    urgency:{score:87,bucket:"high",factors:{deadline_hours:38,effort_minutes:180,slack_hours:35,importance:2,inactivity_days:7,blocked_dependencies:1,availability_minutes:null}},
  }],
  resurfaced_items:[{id:"2",title:"Write docs"}],
  evening_reflection:{status:"awaiting",journal:null},
};

describe("Today",()=>{
  it("renders the briefing, ranked reasons, resurfacing, and reflection state",()=>{
    render(<Today data={data}/>);
    expect(screen.getByRole("heading",{name:"Today"})).toBeInTheDocument();
    expect(screen.getByText("Good morning.")).toBeInTheDocument();
    expect(screen.getByRole("heading",{name:"Word of the Day"})).toBeInTheDocument();
    expect(screen.getByText("inchoate")).toBeInTheDocument();
    expect(screen.getByText(/only partly in existence/)).toBeInTheDocument();
    expect(screen.getAllByText("Submit demo")).toHaveLength(2);
    expect(screen.getByText("deadline: Friday")).toBeInTheDocument();
    expect(screen.getByText("high urgency — score 87")).toBeTruthy();
    expect(screen.getByText("35h slack · 180 min effort")).toBeTruthy();
    expect(screen.getByText("Write docs")).toBeInTheDocument();
    expect(screen.getByText("Evening reflection: awaiting")).toBeInTheDocument();
  });

  it("does not fabricate absent sections",()=>{
    render(<Today data={{...data,briefing:null,power_list:[],open_commitments:[],resurfaced_items:[]}}/>);
    expect(screen.queryByText("Power List")).toBeNull();
    expect(screen.queryByText("Resurfaced")).toBeNull();
  });
});
