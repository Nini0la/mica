import {vi} from "vitest";

// Keep workflow response queues separate from the independent mount-time profile request.
export function mockFetchWithProfile(){
  const requests=vi.fn<typeof fetch>();
  vi.spyOn(globalThis,"fetch").mockImplementation((input,init)=>{
    if(new URL(String(input)).pathname==="/api/companion")return Promise.resolve(new Response(JSON.stringify({name:"Pip",temperament:"calm",version:1,model_mode:"deterministic"}),{status:200,headers:{"content-type":"application/json"}}));
    return requests(input,init);
  });
  return requests;
}
