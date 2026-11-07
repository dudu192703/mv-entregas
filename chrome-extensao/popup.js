const saida = document.getElementById("saida");
document.getElementById("ler").addEventListener("click", async () => {
  try {
    const [tab] = await chrome.tabs.query({active:true,currentWindow:true});
    const url = new URL(tab.url);
    if (url.hostname !== "envios.mercadolivre.com.br" || !url.pathname.startsWith("/logistics/dist-ops/reports/monitoring")) {
      saida.textContent = "Abra primeiro a página de monitoramento do Mercado Livre.";
      return;
    }
    const [res] = await chrome.scripting.executeScript({target:{tabId:tab.id},func:lerPagina});
    saida.textContent = JSON.stringify(res.result,null,2);
  } catch (e) { saida.textContent = "Erro: " + e.message; }
});
function lerPagina() {
  const texto = document.body.innerText;
  const linhas = texto.split(/\n/).map(s=>s.trim()).filter(Boolean);
  const encontrados = [];
  const rx = /^(\d+)\s+de\s+(\d+)\s+unidades?$/i;
  for(let i=0;i<linhas.length;i++){
    const m=linhas[i].match(rx);
    if(!m) continue;
    const nome=linhas[i-1] || "";
    if(!nome || /^(Em progresso|Finalizado|Buscar transportador)$/i.test(nome))continue;
    let falhas=0;
    for(let j=i+1;j<Math.min(i+4,linhas.length);j++){
      if(rx.test(linhas[j]))break;
      const f=linhas[j].match(/(\d+)\s+com\s+falha/i);
      if(f){falhas=Number(f[1]);break;}
    }
    const processados=Number(m[1]),total=Number(m[2]);
    encontrados.push({nome,processados,total,falhas,entregues_estimados:Math.max(0,processados-falhas)});
  }
  return {pagina:document.title,motoristas:encontrados,observacao:encontrados.length?"Confira nomes e números antes de integrar.":"Nenhum cartão identificado. Abra 'Progresso por motorista' e tente novamente."};
}