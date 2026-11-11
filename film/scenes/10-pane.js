export function draw(g,t,D){
  const {P,C,label,text,enter,hex,images,fitImage}=D;
  label(g,C.label);
  enter(g,t,0,()=>{
    text(g,C.title[0],96,241,76,P.text,{weight:600});
    text(g,'The whole',96,325,76,P.text,{weight:600});
    text(g,'operation.',96,409,76,P.cyan,{weight:600});
  });
  C.features.forEach((s,i)=>enter(g,t,.5+i*.2,()=>{
    hex(g,109,482+i*64,8,P.amber,null);
    text(g,s,135,492+i*64,31,P.muted);
  }));
  enter(g,t,.2,()=>{
    text(g,C.evidence,776,196,21,P.cyan,{mono:true});
    const box=fitImage(g,images.paneHealth,776,210,1080,450,{stroke:P.line,lineWidth:2,anchor:'left'});
    text(g,'BUNDLED SKILL',box.x,686,22,P.amber,{mono:true});
    text(g,'bundlebees:bundlebees-operator',box.x+220,686,22,P.text,{mono:true});
  });
}
