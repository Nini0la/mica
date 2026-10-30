export const API=process.env.NEXT_PUBLIC_API_URL??"http://localhost:8000";

export class ApiError extends Error{
  constructor(public status:number,message:string){super(message);this.name="ApiError";}
}

function cookie(name:string){
  if(typeof document==="undefined") return "";
  return document.cookie.split("; ").find(value=>value.startsWith(`${name}=`))?.split("=")[1]??"";
}

export async function api<T>(path:string,init:RequestInit={}):Promise<T>{
  const headers=new Headers(init.headers);
  if(init.body) headers.set("content-type","application/json");
  if(init.method&&init.method!=="GET") headers.set("x-csrf-token",decodeURIComponent(cookie("csrf_token")));
  let response:Response;
  try{response=await fetch(`${API}${path}`,{...init,headers,credentials:"include"});}
  catch{throw new ApiError(0,"Network unavailable");}
  if(!response.ok){
    let message=`Request failed (${response.status})`;
    try{const body=await response.json() as {detail?:string};if(body.detail)message=body.detail;}catch{}
    throw new ApiError(response.status,message);
  }
  if(response.status===204)return undefined as T;
  if(!response.headers.get("content-type")?.includes("application/json"))return undefined as T;
  return response.json() as Promise<T>;
}
