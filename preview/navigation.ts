import { writable } from 'svelte/store'
const value = () => ({url:new URL(location.hash.slice(1)||'/', 'https://preview.invalid'),params:{} as Record<string,string>,route:{id:null},data:{},status:200,error:null,state:{},form:null})
export const page=writable(value())
export const navigating=writable(null)
export const updated={subscribe:writable(false).subscribe,check:async()=>false}
export const goto=async(url:string|URL,_options?:unknown)=>{const path=String(url);if(path.startsWith('/')&&!path.startsWith('//'))location.hash=path}
export const invalidate=async()=>{}; export const invalidateAll=async()=>{}
export const beforeNavigate=()=>{}; export const afterNavigate=()=>{}; export const onNavigate=()=>{}
export const preloadData=async()=>({});export const preloadCode=async()=>{}
export const pushState=()=>{};export const replaceState=()=>{}
export function installNavigation(){
  window.addEventListener('hashchange',()=>page.set(value()))
  document.addEventListener('click',event=>{
    const link=(event.target as Element)?.closest?.('a');const href=link?.getAttribute('href')||''
    if(href.startsWith('/')&&!href.startsWith('//')&&!href.startsWith('/api/')&&!href.startsWith('/showcase/')){
      event.preventDefault();event.stopImmediatePropagation();void goto(href)
    }
  },true)
}
