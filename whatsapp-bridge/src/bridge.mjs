import makeWASocket, {DisconnectReason, generateMessageIDV2, normalizeMessageContent, jidNormalizedUser} from "@whiskeysockets/baileys";
import pino from "pino";
import {createHash, randomUUID} from "node:crypto";

const phone = value => /^\+[1-9]\d{7,14}$/.test(value);
const pn = jid => {
  const normalized=jidNormalizedUser(jid ?? "");
  return /^\d+@s\.whatsapp\.net$/.test(normalized) ? "+"+normalized.split("@")[0] : null;
};

export class Bridge {
  constructor({store,accountId,number,socketFactory=makeWASocket,makeId=generateMessageIDV2,sendTimeout=8000}) {
    if(!phone(number)) throw new Error("Configure the dedicated assistant number in international format");
    Object.assign(this,{store,accountId,number,socketFactory,makeId,sendTimeout});
    this.owner=randomUUID(); this.socket=null; this.status="disconnected"; this.qr=null;
    this.qrAt=0; this.retries=0; this.generation=0; this.reconnectTimer=null;
    this.eventsChain=Promise.resolve(); this.sessionError=null;
    if(store.setting("assistant_number") && store.setting("assistant_number")!==number)
      throw new Error("State directory belongs to another assistant number");
    if(!store.acquire(this.owner)) throw new Error("Another bridge owns this session");
    store.setting("assistant_number",number);
    store.recover();
    this.heartbeat=setInterval(()=>{
      if(!store.acquire(this.owner)) {this.stopSocket(); this.status="lease_lost"}
    },5000);
    this.heartbeat.unref();
  }
  assertLease() {if(!this.store.owns(this.owner)) throw new Error("lease_lost")}
  info() {return {account_id:this.accountId,status:this.status,address:this.status==="connected"?this.number:null,
    qr:this.qr && Date.now()-this.qrAt<60000?this.qr:null,last_error:this.sessionError,
    pending_events:this.store.pendingCount()}}
  async start() {if(this.store.setting("wanted")==="true") await this.connect()}
  async connect() {
    this.assertLease();
    if(this.socket) return this.info();
    this.store.setting("wanted","true");
    this.status="connecting";
    const generation=++this.generation;
    const auth=this.store.authState();
    let socket;
    try {
      socket=this.socketFactory({auth:auth.state,logger:pino({level:"silent"}),
        markOnlineOnConnect:false,syncFullHistory:false,shouldSyncHistoryMessage:()=>false,
        getMessage:async key=>this.store.getMessage(key.id),
      });
    } catch {this.status="reconnecting"; this.retryConnect(); return this.info()}
    this.socket=socket;
    const current=()=>generation===this.generation && this.store.owns(this.owner);
    socket.ev.on("creds.update",()=>{if(current()) auth.saveCreds()});
    socket.ev.on("connection.update",update=>{
      if(!current()) return;
      if(update.qr) {this.qr=update.qr; this.qrAt=Date.now(); this.status="awaiting_pairing"}
      if(update.connection==="open") {
        if(pn(socket.user?.id)!==this.number) {
          this.store.setting("wanted","false"); this.stopSocket(); this.status="identity_mismatch"; return;
        }
        this.status="connected"; this.qr=null; this.retries=0; this.sessionError=null;
      }
      if(update.connection==="close") {
        const code=update.lastDisconnect?.error?.output?.statusCode;
        this.socket=null; this.qr=null; this.generation++;
        if(code===DisconnectReason.loggedOut || code===DisconnectReason.badSession || code===DisconnectReason.multideviceMismatch) {
          this.store.setting("wanted","false"); this.status="logged_out"; this.store.clearAuth();
        } else if(code===DisconnectReason.connectionReplaced || code===DisconnectReason.forbidden) {
          this.store.setting("wanted","false"); this.status="connection_replaced_or_forbidden";
        } else {this.status="reconnecting"; this.retryConnect()}
      }
    });
    socket.ev.on("messages.upsert",event=>{
      if(!current()) return;
      if(pn(socket.user?.id)!==this.number) {this.sessionError="identity_not_verified";return}
      for(const message of event.messages ?? []) this.stageInbound(message);
      this.eventsChain=this.eventsChain.then(()=>this.flushInbound(socket))
        .catch(()=>{this.sessionError="inbound_processing_failed"});
    });
    socket.ev.on("messages.update",updates=>{
      if(!current()) return;
      for(const {key,update} of updates) {
        if(!key.fromMe || !key.id) continue;
        const state=update.status>=4?"read":update.status===3?"delivered":update.status===2?"accepted":null;
        if(state) this.receipt(key.id,state);
      }
    });
    socket.ev.on("message-receipt.update",updates=>{
      if(!current()) return;
      for(const {key,receipt} of updates) if(key.fromMe && key.id) {
        const state=receipt.readTimestamp||receipt.playedTimestamp?"read":receipt.receiptTimestamp?"delivered":null;
        if(state) this.receipt(key.id,state);
      }
    });
    return this.info();
  }
  retryConnect() {
    if(this.reconnectTimer || this.store.setting("wanted")!=="true") return;
    const delay=Math.min(60000,1000*2**Math.min(this.retries++,6));
    this.reconnectTimer=setTimeout(()=>{
      this.reconnectTimer=null;
      if(this.store.setting("wanted")==="true") void this.connect().catch(()=>{this.sessionError="reconnect_failed";this.retryConnect()});
    },delay);
    this.reconnectTimer.unref();
  }
  stopSocket() {
    this.generation++;
    clearTimeout(this.reconnectTimer); this.reconnectTimer=null;
    const socket=this.socket; this.socket=null; this.qr=null;
    socket?.end(new Error("Assistant connection paused"));
  }
  disconnect() {this.store.setting("wanted","false"); this.stopSocket(); this.status="disconnected"; return this.info()}
  async logout() {
    this.store.setting("wanted","false");
    if(this.socket) await this.socket.logout();
    this.stopSocket(); this.store.clearAuth(); this.status="logged_out"; return this.info();
  }
  async send({account_id,from_address,id,to,text}) {
    this.assertLease();
    if(account_id!==this.accountId || from_address!==this.number || !/^[0-9a-f-]{36}$/i.test(id) || !phone(to) || to===this.number ||
       typeof text!=="string" || text.length>3000 ||
       !/^I'm [^\r\n]{1,60}, an AI assistant acting on behalf of [^\r\n]{1,100}\.\n/.test(text))
      throw new Error("invalid_message");
    const existing=this.store.outgoing(id);
    if(!existing && (this.status!=="connected" || !this.socket)) throw new Error("not_connected");
    const prepared=this.store.prepare(id,to,text,existing?.provider_id ?? this.makeId(this.socket.user.id));
    if(!prepared.fresh) return this.delivery(id);
    const socket=this.socket;
    let timer;
    try {
      const sent=await Promise.race([
        socket.sendMessage(to.slice(1)+"@s.whatsapp.net",{text},{messageId:prepared.row.provider_id}),
        new Promise((_,reject)=>{timer=setTimeout(()=>reject(new Error("send_timeout")),this.sendTimeout)}),
      ]);
      if(!sent?.key?.id || sent.key.id!==prepared.row.provider_id) throw new Error("missing_receipt");
      this.store.result(id,"accepted",sent.message);
    } catch {
      // The network may have accepted the message. Never auto-resend this ID.
      this.store.result(id,"uncertain");
    } finally {clearTimeout(timer)}
    return this.delivery(id);
  }
  delivery(id) {
    const row=this.store.outgoing(id);
    if(!row) throw new Error("not_found");
    return {account_id:this.accountId,id,state:row.state==="sending"?"uncertain":row.state,provider_id:row.provider_id};
  }
  receipt(providerId,state) {
    const row=this.store.byProvider(providerId);
    if(!row) return;
    this.store.result(row.id,state);
    this.store.enqueue(`delivery:${providerId}:${state}`,{account_id:this.accountId,
      account_address:this.number,
      event_id:`delivery:${providerId}:${state}`,kind:"delivery",provider_id:providerId,message_id:row.id,state});
  }
  stageInbound(message) {
    if(message.key?.fromMe || !message.key?.id) return;
    const jid=message.key.remoteJid ?? "";
    if(jid.endsWith("@g.us") || jid.includes("broadcast") || jid.includes("newsletter")) return;
    const content=normalizeMessageContent(message.message);
    let text=content?.conversation ?? content?.extendedTextMessage?.text;
    if(typeof text!=="string" && ["imageMessage","documentMessage","audioMessage","videoMessage"].some(key=>content?.[key]))
      text="[Unsupported attachment received; inspect it on the dedicated WhatsApp account.]";
    if(typeof text!=="string" || !text.trim()) return;
    // Never partially interpret an oversized answer as an unqualified completion claim.
    const bounded=text.length>4000?"[Unsupported oversized reply; owner review required]":text;
    const quoted=content?.extendedTextMessage?.contextInfo?.stanzaId ?? null;
    const id="in:"+createHash("sha256").update(JSON.stringify([jid,message.key.id])).digest("hex");
    this.store.stageInbound(id,{jid,alternate:message.key.remoteJidAlt,
      text:bounded,provider_id:message.key.id,quoted_id:quoted});
  }
  async flushInbound(socket=this.socket) {
    if(this.status!=="connected" || !socket) return;
    for(const row of this.store.inboundPending()) {
      const message=JSON.parse(row.body);
      const address=pn(message.jid) ?? pn(message.alternate) ??
        pn(message.jid.endsWith("@lid")?await socket.signalRepository?.lidMapping?.getPNForLID(message.jid):null);
      if(!address || address===this.number) {this.store.deferInbound(row.id);this.sessionError="unresolved_sender";continue}
      this.store.finishInbound(row.id,{account_id:this.accountId,account_address:this.number,event_id:message.provider_id,
        kind:"inbound",address,text:message.text,provider_id:message.provider_id,quoted_id:message.quoted_id});
    }
  }
  async flushEvents(apiUrl,token,fetcher=fetch) {
    this.assertLease();
    await this.flushInbound();
    for(const event of this.store.pending()) {
      try {
        const response=await fetcher(apiUrl+"/api/internal/channel-events",{
          method:"POST",headers:{authorization:"Bearer "+token,"content-type":"application/json"},
          body:event.body,signal:AbortSignal.timeout(20000),redirect:"error",
        });
        if(!response.ok) throw new Error("event_rejected");
        this.store.acknowledged(event.id);
      } catch {this.store.retry(event.id,event.attempts+1); this.sessionError="event_delivery_pending"}
    }
  }
  close() {this.stopSocket(); clearInterval(this.heartbeat); this.store.release(this.owner)}
}
