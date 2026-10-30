import {fireEvent,render,screen} from "@testing-library/react";
import {useState} from "react";
import {describe,expect,it} from "vitest";
import Modal from "../app/modal";

describe("Modal keyboard behavior",()=>{
  it("sets initial focus, traps Tab, closes on Escape, and restores focus",()=>{
    function Harness(){
      const [open,setOpen]=useState(false);
      return <><button onClick={()=>setOpen(true)}>Open</button>{open&&<Modal labelledBy="title" onClose={()=>setOpen(false)}><h2 id="title">Dialog</h2><button data-autofocus>First</button><button>Last</button></Modal>}</>;
    }
    render(<Harness/>);
    const opener=screen.getByRole("button",{name:"Open"});
    opener.focus();
    fireEvent.click(opener);
    const first=screen.getByRole("button",{name:"First"});
    const last=screen.getByRole("button",{name:"Last"});
    expect(first).toHaveFocus();
    last.focus();
    fireEvent.keyDown(document,{key:"Tab"});
    expect(first).toHaveFocus();
    fireEvent.keyDown(document,{key:"Escape"});
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(opener).toHaveFocus();
  });
});
