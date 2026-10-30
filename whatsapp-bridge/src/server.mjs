import {createServer} from "node:http";
import {timingSafeEqual} from "node:crypto";
import {resolve} from "node:path";
import {pathToFileURL} from "node:url";
import {Bridge} from "./bridge.mjs";
import {Store} from "./store.mjs";
import QRCode from "qrcode";

export function handler(bridge,token) {
  return async(req,res)=>{
    const expected=Buffer.from("Bearer "+token), supplied=Buffer.from(req.headers.authorization ?? "");
    res.setHeader("content-type","application/json"); res.setHeader("cache-control","no-store");
    if(supplied.length!==expected.length || !timingSafeEqual(supplied,expected)) {
      res.writeHead(403);res.end('{"error":"unauthorized"}');return;
    }
    try {
      let result;
      if(req.method==="GET" && req.url==="/v1/status") result=bridge.info();
      else if(req.method==="GET" && /^\/v1\/messages\/[0-9a-f-]{36}$/i.test(req.url))
        result=bridge.delivery(req.url.split("/").at(-1));
      else if(req.method==="POST") {
        const chunks=[];let size=0;
        for await(const chunk of req) {size+=chunk.length;if(size>16384) throw new Error("invalid_message");chunks.push(chunk)}
        const input=JSON.parse(Buffer.concat(chunks).toString("utf8")||"{}");
        if(req.url==="/v1/messages") result=await bridge.send(input);
        else if(req.url==="/v1/connect") result=await bridge.connect();
        else if(req.url==="/v1/disconnect") result=bridge.disconnect();
        else if(req.url==="/v1/logout") result=await bridge.logout();
        else throw new Error("not_found");
      } else throw new Error("not_found");
      if(result.qr) {result.qr_image=await QRCode.toDataURL(result.qr);delete result.qr}
      res.end(JSON.stringify(result));
    } catch(error) {
      const known=["not_found","invalid_message","not_connected","idempotency_conflict","lease_lost"];
      const code=known.includes(error.message)?error.message:"bridge_failure";
      res.writeHead(code==="not_found"?404:code==="invalid_message"?400:code==="idempotency_conflict"?409:503);
      res.end(JSON.stringify({error:code,account_id:bridge.accountId}));
    }
  };
}

async function main() {
  const {CHANNEL_ACCOUNT_ID:accountId,CHANNEL_BRIDGE_TOKEN:token,WHATSAPP_ASSISTANT_NUMBER:number}=process.env;
  const apiUrl=(process.env.CORE_API_URL??"http://127.0.0.1:8000").replace(/\/$/,"");
  const api=new URL(apiUrl);
  if(!/^[0-9a-f-]{36}$/i.test(accountId??"") || (token?.length??0)<32 || !number ||
     api.username || api.password || api.search || api.hash ||
     !(api.protocol==="https:" || api.protocol==="http:" && ["127.0.0.1","localhost","[::1]"].includes(api.hostname)))
    throw new Error("Set CHANNEL_ACCOUNT_ID, CHANNEL_BRIDGE_TOKEN, WHATSAPP_ASSISTANT_NUMBER and a trusted CORE_API_URL");
  const store=new Store(resolve(process.env.WHATSAPP_STATE_PATH??"state/whatsapp.db"),accountId);
  const bridge=new Bridge({store,accountId,number});
  const server=createServer(handler(bridge,token));
  server.requestTimeout=10000;
  server.listen(Number(process.env.CHANNEL_BRIDGE_PORT??8081),"127.0.0.1");
  let flushing=false;
  const timer=setInterval(async()=>{
    if(flushing)return;flushing=true;
    try {await bridge.flushEvents(apiUrl,token)} catch {console.error("Channel event forwarding unavailable")}
    finally {flushing=false}
  },2000);
  await bridge.start();
  for(const signal of ["SIGINT","SIGTERM"]) process.once(signal,()=>{
    clearInterval(timer);bridge.close();server.close();
    // Do not clear persisted credentials on a normal process restart.
    setTimeout(()=>process.exit(0),100).unref();
  });
  console.log("WhatsApp bridge started; pairing/status are available only through authenticated endpoints.");
}

if(process.argv[1] && import.meta.url===pathToFileURL(process.argv[1]).href) main().catch(()=>{
  console.error("WhatsApp bridge could not start; check configuration and exclusive session ownership.");
  process.exitCode=1;
});
