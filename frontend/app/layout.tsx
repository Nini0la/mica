import "./style.css";
export const metadata={title:"Mica — Personal AI with follow-through",description:"A personal AI companion with persistent context, research and durable follow-through."};
export default function Layout({children}:{children:React.ReactNode}){return <html lang="en"><body>{process.env.NEXT_PUBLIC_JUDGE_DEMO==="1"&&<div role="note" style={{padding:"10px 20px",background:"#edf5e8",color:"#243b2a",fontSize:"14px"}}>Mica judge demo · Shared synthetic account · Please use fictional details. Powered by NVIDIA Nemotron on Nebius.</div>}{children}</body></html>}
