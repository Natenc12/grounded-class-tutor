const { contextBridge, ipcRenderer } = require('electron');

const actions = ['getState', 'signIn', 'cancelSignIn', 'signOut', 'listModels',
  'chooseFile', 'useSample', 'ask', 'cancelAsk', 'openUsage'];
const bridge = Object.fromEntries(actions.map(name => [name,
  (...args) => ipcRenderer.invoke(`gct:${name}`, ...args)]));
bridge.onState = callback => {
  if (typeof callback !== 'function') throw new TypeError('A callback is required.');
  const listener = (_event, state) => callback(state);
  ipcRenderer.on('gct:state', listener);
  return () => ipcRenderer.removeListener('gct:state', listener);
};
contextBridge.exposeInMainWorld('gct', bridge);
