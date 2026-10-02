import {PGlite} from '@electric-sql/pglite';
import readline from 'node:readline';
const db=await PGlite.create();
const input=readline.createInterface({input:process.stdin});
for await (const line of input) {
 try { const {sql,params,exec}=JSON.parse(line); const result=exec ? await db.exec(sql) : await db.query(sql,params);console.log(JSON.stringify({ok:true,result})); }
 catch(e) {console.log(JSON.stringify({ok:false,error:e.message}));}
}
await db.close();
