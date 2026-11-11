import S from '../settings.json';import copy from '../copy.json';import * as D from './draw.js';
import * as hook from '../scenes/01-hook.js';import * as contracts from '../scenes/02-contracts.js';import * as waves from '../scenes/03-waves.js';import * as dispatch from '../scenes/04-dispatch.js';import * as verify from '../scenes/05-verify.js';import * as retry from '../scenes/06-retry.js';import * as control from '../scenes/07-control.js';import * as boundary from '../scenes/08-boundary.js';import * as proof from '../scenes/09-proof.js';import * as pane from '../scenes/10-pane.js';import * as end from '../scenes/11-end.js';
const modules={hook,contracts,waves,dispatch,verify,retry,control,boundary,proof,pane,end};
const sources={
  banner:'assets/images/banner.png',
  paneHealth:'assets/images/pane-health.png',
  termDoctor:'assets/images/term-doctor.png',
  termPytest:'assets/images/term-pytest.png',
  termSchemas:'assets/images/term-schemas.png',
  orch:'assets/images/orch-state.png',
};
const images=Object.fromEntries(Object.entries(sources).map(([k,src])=>{const i=new Image();i.src=src;return [k,i]}));
const ready=Promise.all([
  document.fonts.load('600 100px Grotesk'),
  document.fonts.load('500 30px Mono'),
  ...Object.values(images).map(i=>i.decode()),
]);
let intensity=1;const mounts=[];
export function mount(){for(const s of S.scenes){const element=document.getElementById(s.id),g=element.querySelector('canvas').getContext('2d',{alpha:false});const render=async(time,signal)=>{await ready;if(signal?.aborted)return;const f=Math.max(0,Math.min(s.end-s.start-1,Math.round(time*S.fps))),t=f/S.fps,T=(s.start+f)/S.fps;g.resetTransform();g.globalAlpha=1;g.globalCompositeOperation='source-over';D.base(g,T,intensity);g.save();modules[s.id].draw(g,t,{...D,C:copy[s.id],T,intensity,images});g.restore()};mounts.push({element,render,...s});window.__hyperframes.registerFrameSource({element,ready,render,dispose(){}})}}
export const test={async seek(f){const m=mounts.find(m=>f>=m.start&&f<m.end);if(!m)throw Error('Frame outside timeline');for(const n of mounts)n.element.style.visibility=n===m?'visible':'hidden';await m.render((f-m.start)/S.fps)},setIntensity(v){intensity=v}};
