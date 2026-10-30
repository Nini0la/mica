import {DatabaseSync} from "node:sqlite";
import {mkdirSync, chmodSync} from "node:fs";
import {dirname} from "node:path";
import {createHash} from "node:crypto";
import {BufferJSON, initAuthCreds, proto} from "@whiskeysockets/baileys";

const encode = value => JSON.stringify(value, BufferJSON.replacer);
const decode = value => value == null ? null : JSON.parse(value, BufferJSON.reviver);

export class Store {
  constructor(path, accountId) {
    if(path !== ":memory:") mkdirSync(dirname(path), {recursive:true, mode:0o700});
    this.db = new DatabaseSync(path);
    if(path !== ":memory:") chmodSync(path, 0o600);
    this.db.exec(`PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL; PRAGMA busy_timeout=3000;
      CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS auth (key TEXT PRIMARY KEY, value TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS outgoing (id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,
        address TEXT NOT NULL, content TEXT NOT NULL, provider_id TEXT UNIQUE NOT NULL,
        state TEXT NOT NULL, message_json TEXT, created_at INTEGER NOT NULL);
      CREATE TABLE IF NOT EXISTS events (id TEXT PRIMARY KEY, body TEXT, attempts INTEGER NOT NULL DEFAULT 0,
        next_attempt INTEGER NOT NULL DEFAULT 0, acknowledged INTEGER NOT NULL DEFAULT 0);
      CREATE TABLE IF NOT EXISTS inbox (id TEXT PRIMARY KEY, body TEXT, processed INTEGER NOT NULL DEFAULT 0,
        next_attempt INTEGER NOT NULL DEFAULT 0);
      CREATE TABLE IF NOT EXISTS lease (id INTEGER PRIMARY KEY CHECK(id=1), owner TEXT NOT NULL, expires INTEGER NOT NULL);`);
    if(this.setting("account_id") && this.setting("account_id") !== accountId)
      throw new Error("State directory belongs to another channel account");
    this.setting("account_id", accountId);
  }
  transaction(work) {
    this.db.exec("BEGIN IMMEDIATE");
    try {const result=work(); this.db.exec("COMMIT"); return result}
    catch(error) {this.db.exec("ROLLBACK"); throw error}
  }
  setting(key, value=undefined) {
    if(value !== undefined) this.db.prepare("INSERT INTO settings VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value").run(key,String(value));
    return this.db.prepare("SELECT value FROM settings WHERE key=?").get(key)?.value;
  }
  acquire(owner, now=Date.now()) {
    return this.transaction(()=>{
      const lease=this.db.prepare("SELECT * FROM lease WHERE id=1").get();
      if(lease && lease.owner!==owner && lease.expires>now) return false;
      this.db.prepare("INSERT INTO lease VALUES (1,?,?) ON CONFLICT(id) DO UPDATE SET owner=excluded.owner,expires=excluded.expires").run(owner,now+30000);
      return true;
    });
  }
  owns(owner) {const row=this.db.prepare("SELECT * FROM lease WHERE id=1").get(); return row?.owner===owner && row.expires>Date.now()}
  release(owner) {this.db.prepare("DELETE FROM lease WHERE owner=?").run(owner)}
  recover() {this.db.prepare("UPDATE outgoing SET state='uncertain' WHERE state='sending'").run()}
  authState() {
    const get=key=>decode(this.db.prepare("SELECT value FROM auth WHERE key=?").get(key)?.value);
    const put=(key,value)=>value==null ? this.db.prepare("DELETE FROM auth WHERE key=?").run(key) :
      this.db.prepare("INSERT INTO auth VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value").run(key,encode(value));
    const creds=get("creds") ?? initAuthCreds();
    put("creds",creds);
    return {state:{creds,keys:{
      get: async(type,ids)=>Object.fromEntries(ids.map(id=>{
        let value=get(`${type}:${id}`);
        if(type==="app-state-sync-key" && value) value=proto.Message.AppStateSyncKeyData.fromObject(value);
        return [id,value];
      })),
      set: async data=>this.transaction(()=>{
        for(const [type,items] of Object.entries(data)) for(const [id,value] of Object.entries(items)) put(`${type}:${id}`,value);
      }),
    }},saveCreds:()=>put("creds",creds)};
  }
  clearAuth() {this.db.exec("DELETE FROM auth")}
  outgoing(id) {return this.db.prepare("SELECT * FROM outgoing WHERE id=?").get(id)}
  byProvider(id) {return this.db.prepare("SELECT * FROM outgoing WHERE provider_id=?").get(id)}
  prepare(id,address,content,providerId) {
    const fingerprint=createHash("sha256").update(JSON.stringify({address,content})).digest("hex");
    return this.transaction(()=>{
      const prior=this.outgoing(id);
      if(prior) {
        if(prior.fingerprint!==fingerprint) throw new Error("idempotency_conflict");
        return {fresh:false,row:prior};
      }
      this.db.prepare("INSERT INTO outgoing(id,fingerprint,address,content,provider_id,state,created_at) VALUES (?,?,?,?,?,'sending',?)").run(id,fingerprint,address,content,providerId,Date.now());
      return {fresh:true,row:this.outgoing(id)};
    });
  }
  result(id,state,message=null) {
    const row=this.outgoing(id);
    const rank={sending:0,uncertain:0,failed:0,accepted:1,delivered:2,read:3};
    if(!row || rank[row.state]>rank[state]) return;
    this.db.prepare("UPDATE outgoing SET state=?,message_json=COALESCE(?,message_json) WHERE id=?").run(state,message?encode(message):null,id);
  }
  getMessage(providerId) {return decode(this.byProvider(providerId)?.message_json)}
  enqueue(id,body) {
    this.db.prepare("INSERT INTO events(id,body) VALUES (?,?) ON CONFLICT(id) DO NOTHING").run(id,JSON.stringify(body));
  }
  stageInbound(id,body) {this.db.prepare("INSERT INTO inbox(id,body) VALUES (?,?) ON CONFLICT(id) DO NOTHING").run(id,JSON.stringify(body))}
  inboundPending() {return this.db.prepare("SELECT * FROM inbox WHERE processed=0 AND next_attempt<=? ORDER BY rowid LIMIT 50").all(Date.now())}
  deferInbound(id) {this.db.prepare("UPDATE inbox SET next_attempt=? WHERE id=?").run(Date.now()+60000,id)}
  finishInbound(id,event) {this.transaction(()=>{this.enqueue(id,event);this.db.prepare("UPDATE inbox SET processed=1,body=NULL WHERE id=?").run(id)})}
  pending(now=Date.now()) {return this.db.prepare("SELECT * FROM events WHERE acknowledged=0 AND next_attempt<=? ORDER BY rowid LIMIT 50").all(now)}
  pendingCount() {return Number(this.db.prepare("SELECT COUNT(*) AS n FROM events WHERE acknowledged=0").get().n)}
  acknowledged(id) {this.db.prepare("UPDATE events SET acknowledged=1,body=NULL WHERE id=?").run(id)}
  retry(id,attempts) {this.db.prepare("UPDATE events SET attempts=?,next_attempt=? WHERE id=?").run(attempts,Date.now()+Math.min(300000,1000*2**Math.min(attempts,8)),id)}
  close() {this.db.close()}
}
