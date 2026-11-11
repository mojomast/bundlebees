export function draw(g,t,D){
  const {P,C,label,illustrated,text,enter,ease,smooth,hex,bee,images,fitImage}=D;
  label(g,C.label);illustrated(g);
  const swap=smooth((t-2.9)/.5);
  g.save();g.globalAlpha=1-swap;
  enter(g,t,0,()=>{text(g,C.title[0],96,300,102,P.text,{weight:600});text(g,C.title[1],96,420,102,P.text,{weight:600})});
  g.restore();
  g.save();g.globalAlpha=swap;const dy=22*(1-swap);
  text(g,'Give it',96,302+dy,136,P.text,{weight:600});
  text(g,'a hive.',96,454+dy,136,P.amber,{weight:600});
  g.restore();
  text(g,'A coding swarm. Under your direction.',100,565,33,P.muted);
  g.save();g.globalAlpha=.25+.75*smooth((t-.35)/.7);
  text(g,'PRODUCT BANNER',1020,150,18,P.cyan,{mono:true});
  fitImage(g,images.banner,1020,164,800,400,{stroke:P.line,lineWidth:2,anchor:'left'});
  g.restore();
  const grow=ease((t-.8)/2.2),cx=1420,cy=640;
  for(let i=0;i<6;i++){
    const a=i*Math.PI/3+Math.PI/6,r=58*grow,x=cx+Math.cos(a)*r,y=cy+Math.sin(a)*r*.35;
    g.save();g.globalAlpha=.15+.55*grow;
    hex(g,x,y,18*grow,null,P.line,1);
    g.restore();
  }
  g.save();g.globalAlpha=ease((t-1)/.65);bee(g,cx,cy,1.05,0,P.amber,Math.sin(t*10));g.restore();
}
