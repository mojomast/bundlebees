export function draw(g,t,D){
  const {P,C,label,title,text,enter,line,images,fitImage}=D;
  label(g,C.label);
  title(g,C.title,t,196,52);
  enter(g,t,.08,()=>{
    text(g,'4',96,320,96,P.amber,{weight:600});
    text(g,'TOOLS',170,304,24,P.amber,{mono:true});
  });
  C.tools.forEach((s,i)=>enter(g,t,.12+i*.08,()=>text(g,s,96,360+i*32,22,P.text,{mono:true})));
  enter(g,t,.22,()=>{
    text(g,'571',96,560,88,P.cyan,{weight:600});
    text(g,'o200k tokens',290,528,24,P.text,{mono:true});
    text(g,'measured',290,560,24,P.muted,{mono:true});
  });
  enter(g,t,.3,()=>{
    text(g,'150',96,678,72,P.text,{weight:600});
    text(g,'tests passed',250,666,24,P.muted,{mono:true});
  });
  line(g,620,220,620,700,P.line,2);
  enter(g,t,.18,()=>fitImage(g,images.termSchemas,650,222,1174,118,{anchor:'left'}));
  enter(g,t,.28,()=>fitImage(g,images.termDoctor,650,352,1174,166,{anchor:'left'}));
  enter(g,t,.38,()=>fitImage(g,images.termPytest,650,530,1174,170,{anchor:'left'}));
}
