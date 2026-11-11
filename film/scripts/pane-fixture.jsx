import React from 'react';
import {createRoot} from 'react-dom/client';
import {createPane} from '../../desktop/plugin.js';
// Actual pane component and CSS, with a local SDK adapter. No worker connection.
const fixture={
 '/health':{ok:true,health:{}},
 '/models':{ok:true,models:['deepseek-v4.1-flash']},
 '/projects':{ok:true,projects:[{key:'bundlebees',repo:'https://github.com/mojomast/bundlebees',sync_branch:'feat/v0.1.0'}]},
 '/attempts':{ok:true,attempts:[]},
 '/verify-results':{ok:true,results:[]}
};
const Pane=createPane({rest:async p=>fixture[p]||{ok:true}});
createRoot(document.getElementById('root')).render(React.createElement(Pane));
