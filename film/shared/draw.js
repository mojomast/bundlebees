import P from '../palette.json';
export {P};
export const clamp=(x,a=0,b=1)=>Math.min(b,Math.max(a,x));
export const ease=x=>1-(1-clamp(x))**4;
export const smooth=x=>{x=clamp(x);return x*x*(3-2*x)};
export const mix=(a,b,t)=>a+(b-a)*t;
export function line(g,x1,y1,x2,y2,color=P.line,w=2){g.beginPath();g.moveTo(x1,y1);g.lineTo(x2,y2);g.strokeStyle=color;g.lineWidth=w;g.stroke()}
export function poly(g,points,fill,stroke,w=2){g.beginPath();points.forEach(([x,y],i)=>i?g.lineTo(x,y):g.moveTo(x,y));g.closePath();if(fill){g.fillStyle=fill;g.fill()}if(stroke){g.strokeStyle=stroke;g.lineWidth=w;g.stroke()}}
export function hex(g,x,y,r,fill=null,stroke=P.line,w=2,rot=0){poly(g,Array.from({length:6},(_,i)=>[x+r*Math.cos(rot+i*Math.PI/3),y+r*Math.sin(rot+i*Math.PI/3)]),fill,stroke,w)}
export function text(g,s,x,y,size=32,color=P.text,{weight=500,mono=false,align='left',alpha=1}={}){g.save();g.globalAlpha*=alpha;g.font=`${weight} ${size}px ${mono?'Mono':'Grotesk'}`;g.fillStyle=color;g.textAlign=align;g.textBaseline='alphabetic';g.fillText(s,x,y);g.restore()}
export function enter(g,t,start,draw){const p=ease((t-start)/.75);g.save();g.globalAlpha*=p;g.translate(0,30*(1-p));draw();g.restore()}
export function label(g,s){text(g,s,96,94,23,P.amber,{mono:true});line(g,96,119,1824,119,P.line,1)}
export function title(g,s,t,y=230,size=80){enter(g,t,0,()=>text(g,s,96,y,size,P.text,{weight:600}))}
export function note(g,s,y=672,color=P.muted){text(g,s,96,y,27,color)}
export function illustrated(g){text(g,'ILLUSTRATED WORKFLOW',1824,94,20,P.muted,{mono:true,align:'right'})}
export function base(g,T,intensity=1){g.fillStyle=P.background;g.fillRect(0,0,1920,1080);g.save();g.beginPath();g.rect(0,0,1920,710);g.clip();g.globalAlpha=.16*intensity;const shift=10*Math.sin(T*.13);for(let row=-1;row<5;row++)for(let col=0;col<6;col++)hex(g,1280+col*152+shift,80+row*176+(col%2)*88,101,null,P.line,1);g.restore()}
export function bee(g,x,y,scale=1,angle=0,color=P.amber,wing=0){g.save();g.translate(x,y);g.rotate(angle);g.scale(scale,scale);const w=1+wing*.12;poly(g,[[-9,-9],[-42,-37*w],[-22,-42*w],[8,-17]],'#162d36',P.cyan,1.5);poly(g,[[-8,10],[-42,37*w],[-22,42*w],[8,17]],'#162d36',P.cyan,1.5);poly(g,[[7,-10],[31,-36*w],[41,-26*w],[20,-4]],'#162d36',P.cyan,1.5);poly(g,[[7,10],[31,36*w],[41,26*w],[20,4]],'#162d36',P.cyan,1.5);hex(g,0,0,25,color,null);g.fillStyle=P.background;g.fillRect(-11,-23,5,46);g.fillRect(4,-23,5,46);hex(g,31,0,11,color,null);poly(g,[[-22,-8],[-34,0],[-22,8]],color);g.restore()}
export function route(g,points,color=P.line,w=3,progress=1){g.save();g.beginPath();g.moveTo(...points[0]);g.bezierCurveTo(...points[1],...points[2],...points[3]);g.strokeStyle=color;g.lineWidth=w;if(progress<1){g.setLineDash([1400*progress,1400]);}g.stroke();g.restore()}
export function curve(points,p){const q=1-p;return [0,1].map(k=>q*q*q*points[0][k]+3*q*q*p*points[1][k]+3*q*p*p*points[2][k]+p*p*p*points[3][k])}
export function fly(g,points,p,T,color=P.amber,scale=1){const [x,y]=curve(points,p),[x2,y2]=curve(points,Math.min(1,p+.005)),angle=p>.995?0:Math.atan2(y2-y,x2-x);bee(g,x,y,scale,angle,color,Math.sin(T*16))}
export function card(g,x,y,w,h,stroke=P.line){poly(g,[[x,y],[x+w-24,y],[x+w,y+24],[x+w,y+h],[x,y+h]],P.surface,stroke,2)}
export function tick(g,x,y,color=P.cyan){line(g,x-10,y,x-2,y+8,color,4);line(g,x-2,y+8,x+14,y-11,color,4)}
