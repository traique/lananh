import {PGlite} from '@electric-sql/pglite';
import readline from 'node:readline';
const db=await PGlite.create();
const input=readline.createInterface({input:process.stdin});
for await (const line of input) {
 try { const {sql,params,exec}=JSON.parse(line);
 // bytea: Python gửi {__bytes__: hex}; pglite cần Uint8Array cho tham số bytea.
 const args=(params||[]).map(v=>v&&typeof v==='object'&&'__bytes__' in v?Uint8Array.from(Buffer.from(v.__bytes__,'hex')):v); const result=exec ? await db.exec(sql) : await db.query(sql,args);console.log(JSON.stringify({ok:true,result},(k,v)=>v instanceof Uint8Array?Buffer.from(v).toString('hex'):v)); }
 catch(e) {console.log(JSON.stringify({ok:false,error:e.message}));}
}
await db.close();
