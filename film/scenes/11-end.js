export function draw(g,t,D){
  const {P,C,label,text,enter,hex,bee,smooth,line,images,fitImage}=D;
  label(g,C.label);
  const p=smooth(t/1.5);
  for(let i=0;i<6;i++){
    const a=i*Math.PI/3,x=1660+Math.cos(a)*90*(1-p),y=280+Math.sin(a)*90*(1-p);
    g.save();g.globalAlpha=(1-p)*.8;hex(g,x,y,40,null,P.cyan,2);g.restore();
  }
  bee(g,1660,280,2.2,0,P.amber,0);
  enter(g,t,.08,()=>{
    text(g,'PRODUCT BANNER',96,156,18,P.cyan,{mono:true});
    fitImage(g,images.banner,96,168,640,320,{stroke:P.line,lineWidth:2,anchor:'left'});
  });
  enter(g,t,.15,()=>{
    text(g,C.title,96,560,86,P.text,{weight:600});
    text(g,C.tagline,104,608,34,P.amber,{weight:500});
    line(g,104,628,1140,628,P.line,2);
    text(g,C.url,104,664,28,P.cyan,{mono:true});
    text(g,C.version,104,696,22,P.muted,{mono:true});
    text(g,C.credit,1824,696,20,P.muted,{align:'right'});
  });
}
