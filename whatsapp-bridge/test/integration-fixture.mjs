// Test-only server: real bridge/store/HTTP protocol, an explicitly simulated WhatsApp socket.
// Never imported by src/server.mjs or the production start command.
import {createServer} from "node:http";
import {EventEmitter} from "node:events";
import {Bridge} from "../src/bridge.mjs";
import {Store} from "../src/store.mjs";
import {handler} from "../src/server.mjs";

const {CHANNEL_ACCOUNT_ID:accountId,CHANNEL_BRIDGE_TOKEN:token,WHATSAPP_ASSISTANT_NUMBER:number}=process.env;
const store=new Store(process.env.WHATSAPP_STATE_PATH,accountId);
const socket={ev:new EventEmitter(),user:{id:number.slice(1)+"@s.whatsapp.net"},end(){},async logout(){},
  async sendMessage(_to,content,options){return {key:{id:options.messageId},message:{conversation:content.text}}},
};
const bridge=new Bridge({store,accountId,number,socketFactory:()=>socket});
await bridge.connect();socket.ev.emit("connection.update",{connection:"open"});
const real=handler(bridge,token);
const server=createServer(async(req,res)=>{
  if(req.url!=="/test/inbound")return real(req,res);
  if(req.headers.authorization!=="Bearer "+token){res.writeHead(403);res.end();return}
  let content="";for await(const chunk of req)content+=chunk;
  const data=JSON.parse(content);
  bridge.stageInbound({key:{id:data.id,remoteJid:data.from.slice(1)+"@s.whatsapp.net"},message:{conversation:data.text}});
  await bridge.flushEvents(process.env.CORE_API_URL,token);
  res.setHeader("content-type","application/json");res.end(JSON.stringify({pending:store.pendingCount()}));
});
server.listen(Number(process.env.CHANNEL_BRIDGE_PORT),"127.0.0.1");
process.on("SIGTERM",()=>{bridge.close();server.close();setTimeout(()=>process.exit(0),50)});
