import {test} from "node:test";
import assert from "node:assert/strict";
import {EventEmitter,once} from "node:events";
import {mkdtempSync,rmSync} from "node:fs";
import {tmpdir} from "node:os";
import {join} from "node:path";
import {randomUUID} from "node:crypto";
import {createServer} from "node:http";
import {Store} from "../src/store.mjs";
import {Bridge} from "../src/bridge.mjs";
import {handler} from "../src/server.mjs";

const accountId="aaaaaaaa-aaaa-4aaa-aaaa-aaaaaaaaaaaa";
const number="+15551230000";
const recipient="+15551230001";
const body="I'm Mica, an AI assistant acting on behalf of Morgan.\nPlease deliver the document.";
function socket() {
  const value={ev:new EventEmitter(),user:{id:"15551230000:1@s.whatsapp.net"},sent:[],
    signalRepository:{lidMapping:{getPNForLID:async()=>"15551230001@s.whatsapp.net"}},
    end(){},async logout(){},
    async sendMessage(address,content,options){this.sent.push({address,content,options});return {key:{id:options.messageId},message:{conversation:content.text}}},
  };
  return value;
}
function setup(t,options={}) {
  const folder=mkdtempSync(join(tmpdir(),"pet-bridge-test-"));
  const path=join(folder,"state.db");
  const store=new Store(path,accountId);
  const sockets=[];
  const bridge=new Bridge({store,accountId,number,...options,socketFactory:config=>{
    const result=socket();result.config=config;sockets.push(result);return result;
  }});
  t.after(()=>{bridge.close();store.close();rmSync(folder,{recursive:true,force:true})});
  return {bridge,store,path,sockets};
}
async function connect(bridge,sockets) {await bridge.connect();sockets.at(-1).ev.emit("connection.update",{connection:"open"});assert.equal(bridge.status,"connected")}

test("credentials and signal keys persist transactionally across restart",async t=>{
  const {store,path}=setup(t);
  const auth=store.authState();auth.state.creds.registered=true;auth.saveCreds();
  await auth.state.keys.set({session:{"recipient":Buffer.from([1,2,3])},"app-state-sync-key":{key:{keyData:Buffer.from([4,5])}}});
  const restarted=new Store(path,accountId);
  try {
    const restored=restarted.authState();
    assert.equal(restored.state.creds.registered,true);
    assert.deepEqual((await restored.state.keys.get("session",["recipient"])).recipient,Buffer.from([1,2,3]));
    assert.deepEqual(Buffer.from((await restored.state.keys.get("app-state-sync-key",["key"])).key.keyData),Buffer.from([4,5]));
    await restored.state.keys.set({session:{recipient:null}});
    assert.equal((await restored.state.keys.get("session",["recipient"])).recipient,null);
    const circular={};circular.self=circular;
    await assert.rejects(restored.state.keys.set({session:{good:"new",bad:circular}}));
    assert.equal((await restored.state.keys.get("session",["good"])).good,null);
  } finally {restarted.close()}
});

test("session lease prevents two active bridge processes",t=>{
  const {path}=setup(t);
  const second=new Store(path,accountId);
  try {assert.throws(()=>new Bridge({store:second,accountId,number}),/Another bridge/)} finally {second.close()}
  assert.throws(()=>new Store(path,randomUUID()),/another channel/);
});

test("QR pairing, persisted intent and logged-out state are honest",async t=>{
  const {bridge,store,sockets}=setup(t);
  await bridge.connect();sockets[0].ev.emit("connection.update",{qr:"synthetic-pairing-qr"});
  assert.equal(bridge.info().status,"awaiting_pairing");assert.equal(bridge.info().qr,"synthetic-pairing-qr");
  sockets[0].ev.emit("connection.update",{connection:"open"});assert.equal(bridge.info().qr,null);
  assert.equal(store.setting("wanted"),"true");
  sockets[0].ev.emit("connection.update",{connection:"close",lastDisconnect:{error:{output:{statusCode:401}}}});
  assert.equal(bridge.status,"logged_out");assert.equal(store.setting("wanted"),"false");assert.equal(bridge.reconnectTimer,null);
});

test("temporary disconnect reconnects using stored auth without duplicate sockets",async t=>{
  const {bridge,sockets}=setup(t);
  await connect(bridge,sockets);
  await bridge.connect();assert.equal(sockets.length,1);
  sockets[0].ev.emit("connection.update",{connection:"close",lastDisconnect:{error:{output:{statusCode:428}}}});
  assert.equal(bridge.status,"reconnecting");assert.ok(bridge.reconnectTimer);
  await new Promise(resolve=>setTimeout(resolve,1100));
  assert.equal(sockets.length,2);
  sockets[1].ev.emit("connection.update",{connection:"open"});assert.equal(bridge.status,"connected");
  sockets[0].ev.emit("connection.update",{connection:"close"});assert.equal(bridge.status,"connected");
});

test("wrong assistant phone fails closed",async t=>{
  const {bridge,store,sockets}=setup(t);await bridge.connect();sockets[0].user.id="15559999999@s.whatsapp.net";
  sockets[0].ev.emit("messages.upsert",{messages:[{key:{id:"wrong-account-inbound",remoteJid:"15551230001@s.whatsapp.net"},message:{conversation:"Private data from a wrong account"}}]});
  await bridge.eventsChain;
  assert.equal(store.inboundPending().length,0);
  sockets[0].ev.emit("connection.update",{connection:"open"});
  assert.equal(bridge.status,"identity_mismatch");
  await assert.rejects(bridge.send({account_id:accountId,from_address:number,id:randomUUID(),to:recipient,text:body}),/not_connected/);
});

test("outgoing IDs are stable, replayable, and content-bound",async t=>{
  const {bridge,sockets}=setup(t);await connect(bridge,sockets);
  const message={account_id:accountId,from_address:number,id:randomUUID(),to:recipient,text:body};
  const first=await bridge.send(message);const duplicate=await bridge.send(message);
  assert.deepEqual(first,duplicate);assert.equal(sockets[0].sent.length,1);assert.equal(first.state,"accepted");
  assert.equal(sockets[0].sent[0].options.messageId,first.provider_id);
  await assert.rejects(bridge.send({...message,text:body+"different"}),/idempotency_conflict/);
  assert.equal(sockets[0].sent.length,1);
});

test("interrupted send is uncertain after restart and is never resent",async t=>{
  const {bridge,store,path,sockets}=setup(t);await connect(bridge,sockets);
  const id=randomUUID();store.prepare(id,recipient,body,"provider-stable");
  bridge.close();
  const reopened=new Store(path,accountId);const recovered=new Bridge({store:reopened,accountId,number,socketFactory:socket});
  try {
    assert.equal(recovered.delivery(id).state,"uncertain");
    const outcome=await recovered.send({account_id:accountId,from_address:number,id,to:recipient,text:body});
    assert.equal(outcome.state,"uncertain");assert.equal(sockets[0].sent.length,0);
  } finally {recovered.close();reopened.close()}
});

test("timeout and transport exceptions do not claim failure-to-send or retry blindly",async t=>{
  const {bridge,sockets}=setup(t,{sendTimeout:20});await connect(bridge,sockets);
  sockets[0].sendMessage=async()=>new Promise(()=>{});
  const message={account_id:accountId,from_address:number,id:randomUUID(),to:recipient,text:body};
  assert.equal((await bridge.send(message)).state,"uncertain");
  assert.equal((await bridge.send(message)).state,"uncertain");
});

test("inbound events survive restart, normalize LID senders and deduplicate",async t=>{
  const {bridge,store,path,sockets}=setup(t);await connect(bridge,sockets);
  const message={key:{id:"inbound-1",remoteJid:"12345@lid",fromMe:false},message:{extendedTextMessage:{text:"DONE: report",contextInfo:{stanzaId:"quote-1"}}}};
  bridge.stageInbound(message);bridge.stageInbound(message);bridge.close();
  const reopened=new Store(path,accountId), connected=[];
  const recovered=new Bridge({store:reopened,accountId,number,socketFactory:()=>{const s=socket();connected.push(s);return s}});
  try {
    await connect(recovered,connected);await recovered.flushInbound();
    assert.equal(reopened.pending().length,1);
    const event=JSON.parse(reopened.pending()[0].body);
    assert.equal(event.address,recipient);assert.equal(event.quoted_id,"quote-1");assert.equal(event.text,"DONE: report");
    await recovered.flushEvents("http://localhost:8000","t".repeat(48),async(_url,options)=>{
      assert.equal(options.redirect,"error");assert.equal(options.headers.authorization,"Bearer "+"t".repeat(48));
      return {ok:true};
    });
    assert.equal(reopened.pendingCount(),0);
    recovered.stageInbound(message);await recovered.flushInbound();assert.equal(reopened.pendingCount(),0);
  } finally {recovered.close();reopened.close()}
  assert.equal(store.pendingCount(),0);
});

test("failed inbound forwarding is retained and retried without emitting new events",async t=>{
  const {bridge,store,sockets}=setup(t);await connect(bridge,sockets);
  bridge.stageInbound({key:{id:"reply",remoteJid:"15551230001@s.whatsapp.net"},message:{conversation:"AGREE"}});
  await bridge.flushEvents("http://localhost:8000","t".repeat(48),async()=>({ok:false}));
  assert.equal(store.pendingCount(),1);assert.equal(store.pending().length,0);
  store.db.prepare("UPDATE events SET next_attempt=0").run();
  await bridge.flushEvents("http://localhost:8000","t".repeat(48),async()=>({ok:true}));
  assert.equal(store.pendingCount(),0);
  assert.equal(store.db.prepare("SELECT body FROM events").get().body,null);
});

test("ignore bot echoes and groups; monotonic receipts do not mean task completion",async t=>{
  const {bridge,store,sockets}=setup(t);await connect(bridge,sockets);
  bridge.stageInbound({key:{id:"echo",remoteJid:"15551230001@s.whatsapp.net",fromMe:true},message:{conversation:"DONE"}});
  bridge.stageInbound({key:{id:"group",remoteJid:"123@g.us"},message:{conversation:"DONE"}});
  await bridge.flushInbound();assert.equal(store.pendingCount(),0);
  const sent=await bridge.send({account_id:accountId,from_address:number,id:randomUUID(),to:recipient,text:body});
  bridge.receipt(sent.provider_id,"read");bridge.receipt(sent.provider_id,"delivered");
  assert.equal(bridge.delivery(sent.id).state,"read");
});

test("HTTP boundary authenticates, renders QR privately, rejects changed retry payload",async t=>{
  const {bridge,sockets}=setup(t);await bridge.connect();
  sockets[0].ev.emit("connection.update",{qr:"synthetic-qr"});
  const token="t".repeat(48),server=createServer(handler(bridge,token));
  server.listen(0,"127.0.0.1");await once(server,"listening");
  t.after(()=>server.close());const base=`http://127.0.0.1:${server.address().port}`;
  assert.equal((await fetch(base+"/v1/status")).status,403);
  const response=await fetch(base+"/v1/status",{headers:{authorization:"Bearer "+token}});
  assert.equal(response.headers.get("cache-control"),"no-store");
  const status=await response.json();assert.match(status.qr_image,/^data:image\/png;base64,/);assert.equal(status.qr,undefined);
  sockets[0].ev.emit("connection.update",{connection:"open"});
  const data={account_id:accountId,from_address:number,id:randomUUID(),to:recipient,text:body};
  const send=payload=>fetch(base+"/v1/messages",{method:"POST",headers:{authorization:"Bearer "+token,"content-type":"application/json"},body:JSON.stringify(payload)});
  assert.equal((await send(data)).status,200);assert.equal((await send({...data,text:body+"changed"})).status,409);
  assert.equal((await send({...data,id:randomUUID(),account_id:randomUUID()})).status,400);
  assert.equal((await send({...data,id:randomUUID(),from_address:recipient})).status,400);
  assert.equal((await send({...data,id:randomUUID(),text:"I'm Morgan, do this now"})).status,400);
});
