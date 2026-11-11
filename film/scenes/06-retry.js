export function draw(g,t,D){
  const {P,C,label,illustrated,text,hex,line,route,fly,smooth,tick,note}=D;
  label(g,C.label);illustrated(g);
  const change=smooth((t-3.6)/.5);
  g.save();g.globalAlpha=1-change;
  text(g,C.title[0],96,220,72,P.text,{weight:600});
  text(g,C.title[1],96,300,72,P.amber,{weight:600});
  g.restore();
  g.save();g.globalAlpha=change;
  text(g,C.resolved,96,252,64,P.text,{weight:600});
  g.restore();
  const back=[[850,480],[760,350],[340,340],[780,200]],
        out=[[850,480],[1000,610],[1200,610],[1450,480]],
        success=[[850,480],[1050,480],[1220,480],[1450,480]];
  route(g,back,P.reject,2);route(g,out,P.amber,2);route(g,success,P.line,3);
  hex(g,850,480,85,P.surface,P.cyan,2);
  hex(g,1450,480,85,P.surface,P.cyan,2);
  text(g,'VERIFY',850,604,26,P.cyan,{mono:true,align:'center'});
  text(g,'FEATURE BRANCH',1450,604,26,P.cyan,{mono:true,align:'center'});
  if(t<1.9) fly(g,back,smooth(t/1.9),t,P.reject,1);
  else if(t<4) fly(g,out,smooth((t-1.9)/2.1),t,P.amber,1);
  else {fly(g,success,smooth((t-4)/2),t,P.amber,1);tick(g,850,480);text(g,'ACCEPTED',1120,411,26,P.cyan,{mono:true,align:'center'})}
  line(g,1450,480,1700,480,P.cyan,2);line(g,1700,480,1758,422,P.line,2);
  text(g,'OWNER',1742,559,22,P.muted,{mono:true,align:'center'});
  text(g,'BB02  rejected  att_8d589c81592c66a7  ·  child_env allowlist',96,656,21,P.reject,{mono:true});
  text(g,'retried  att_238d7f7d39f31891  ·  merged  da2aff1',96,684,21,P.amber,{mono:true});
  text(g,C.note,1824,684,21,P.muted,{align:'right'});
}
