const TARGET = "https://envios.mercadolivre.com.br/logistics/dist-ops/reports/monitoring";
async function sync() {
  const {enabled=false,token=""} = await chrome.storage.local.get(["enabled","token"]);
  if(!enabled || token.length<24) return {ok:false,erro:"Configure o token e ative a sincronização."};
  const tabs=await chrome.tabs.query({url:TARGET+"*"});
  if(!tabs.length) return {ok:false,erro:"Abra a página de monitoramento do Mercado Livre no Chrome."};
  for(const tab of tabs) {
    try {
      const [result]=await chrome.scripting.executeScript({target:{tabId:tab.id},func:lerPagina});
      const motoristas=result.result.motoristas;
      if(!motoristas.length)continue;
      const response=await fetch("http://127.0.0.1:8000/api/ml/sync",{
        method:"POST",headers:{"Content-Type":"application/json","X-MV-Sync-Token":token},
        body:JSON.stringify({motoristas})
      });
      if(!response.ok) return {ok:false,erro:"Servidor MV Entregas respondeu HTTP "+response.status+" — "+(await response.text()).slice(0,200)};
      return await response.json();
    }catch(e){return {ok:false,erro:String(e.message||e)};}
  }
  return {ok:false,erro:"Abra 'Progresso por motorista' antes de sincronizar."};
}
function lerPagina(){
  const linhas=document.body.innerText.split(/\n/).map(x=>x.trim()).filter(Boolean), motoristas=[];
  const rx=/^(\d+)\s+de\s+(\d+)\s+unidades?$/i;
  for(let i=0;i<linhas.length;i++){
    const m=linhas[i].match(rx);if(!m)continue;
    const nome=linhas[i-1]||"";if(!nome||/^(Em progresso|Finalizado|Buscar transportador)$/i.test(nome))continue;
    let falhas=0;
    for(let j=i+1;j<Math.min(i+4,linhas.length);j++){
      if(rx.test(linhas[j]))break;
      const f=linhas[j].match(/(\d+)\s+com\s+falha/i);if(f){falhas=Number(f[1]);break;}
    }
    motoristas.push({nome,processados:Number(m[1]),total:Number(m[2]),falhas});
  }
  return {motoristas};
}
chrome.alarms.onAlarm.addListener(async alarm=>{
  if(alarm.name!=="mv-sync")return;
  const result=await sync();await chrome.storage.local.set({lastResult:result,lastAt:new Date().toLocaleString("pt-BR")});
});
chrome.runtime.onMessage.addListener((msg,sender,sendResponse)=>{
  if(msg.type!=="sync-now")return;
  sync().then(async result=>{await chrome.storage.local.set({lastResult:result,lastAt:new Date().toLocaleString("pt-BR")});sendResponse(result);}).catch(e=>sendResponse({ok:false,erro:e.message}));
  return true;
});
chrome.runtime.onInstalled.addListener(async()=>{const {enabled}=await chrome.storage.local.get("enabled");if(enabled)await chrome.alarms.create("mv-sync",{periodInMinutes:1});});
