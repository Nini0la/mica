"use client";
import {ReactNode,useEffect,useRef} from "react";

const focusable="button:not([disabled]),[href],input:not([disabled]),select:not([disabled]),textarea:not([disabled]),[tabindex]:not([tabindex='-1'])";

export default function Modal({labelledBy,onClose,required=false,children,className=""}:{labelledBy:string;onClose:()=>void;required?:boolean;children:ReactNode;className?:string}){
  const ref=useRef<HTMLElement>(null);
  useEffect(()=>{
    const previous=document.activeElement as HTMLElement|null;
    const node=ref.current;
    (node?.querySelector<HTMLElement>("[data-autofocus]")??node?.querySelector<HTMLElement>(focusable)??node)?.focus();
    function keydown(event:KeyboardEvent){
      if(event.key==="Escape"&&!required){event.preventDefault();onClose();return;}
      if(event.key!=="Tab"||!node)return;
      const elements=Array.from(node.querySelectorAll<HTMLElement>(focusable)).filter(element=>!element.hidden);
      if(!elements.length){event.preventDefault();node.focus();return;}
      const first=elements[0],last=elements[elements.length-1];
      if(event.shiftKey&&document.activeElement===first){event.preventDefault();last.focus();}
      else if(!event.shiftKey&&document.activeElement===last){event.preventDefault();first.focus();}
    }
    document.addEventListener("keydown",keydown);
    return()=>{document.removeEventListener("keydown",keydown);previous?.focus();};
  },[onClose,required]);
  return <div className="modal-backdrop"><section ref={ref} className={`modal ${className}`} role="dialog" aria-modal="true" aria-labelledby={labelledBy} tabIndex={-1}>{children}</section></div>;
}
