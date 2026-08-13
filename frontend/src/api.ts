export const API=import.meta.env.VITE_API_BASE_URL || '/api/v1';

export function idem(){return `web-${Date.now()}-${Math.random().toString(36).slice(2)}`}
export async function request<T>(path:string, init?:RequestInit):Promise<T>{
  const r=await fetch(`${API}${path}`,init); const text=await r.text(); let body:any={};
  try{body=text?JSON.parse(text):{}}catch{body={raw:text}}
  if(!r.ok){const e=body?.error;throw new Error(e?.code?`${e.code}: ${e.message||''}`:JSON.stringify(body))}
  return body as T;
}
export function jsonInit(method:string, body:any, withIdem=false):RequestInit{
  const headers:Record<string,string>={'Content-Type':'application/json'}; if(withIdem)headers['Idempotency-Key']=idem();
  return {method,headers,body:JSON.stringify(body)};
}
