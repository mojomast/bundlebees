export function draw(g,t,D){
  const {P,C,label,title,text,images,fitImage,enter}=D;
  label(g,C.label);
  text(g,'REAL ORCH STATE',1824,94,20,P.cyan,{mono:true,align:'right'});
  title(g,C.title,t,192,52);
  text(g,C.note,1824,192,22,P.muted,{align:'right'});
  enter(g,t,.25,()=>{
    fitImage(g,images.orch,96,222,1728,478,{stroke:P.line,lineWidth:2});
  });
}
