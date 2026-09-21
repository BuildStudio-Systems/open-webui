<script lang="ts">
  import StudioLanguagePicker from '$lib/components/common/StudioLanguagePicker.svelte'
  import {setContext} from 'svelte'
  import i18n,{initI18n} from '$lib/i18n'
  import {config,user,models,showSidebar,showSettings,settings,knowledge,tools,skills,functions,socket} from '$lib/stores'
  import Sidebar from '$lib/components/layout/Sidebar.svelte'
  import SettingsModal from '$lib/components/chat/SettingsModal.svelte'
  import Workspace from '../src/routes/(app)/workspace/+layout.svelte'
  import Admin from '../src/routes/(app)/admin/+layout.svelte'
  import {page,goto} from './navigation'
  import {sampleUser,sampleModels,sampleConfig} from './fixtures'
  setContext('i18n',i18n);initI18n('en-US')
  user.set(sampleUser as any);config.set(sampleConfig as any);models.set(sampleModels as any)
  settings.set({models:['sample-assistant'],showUsername:true});showSidebar.set(true)
  knowledge.set([]);tools.set([]);skills.set([]);functions.set([])
  socket.set({on(){},off(){},emit(){},connected:false} as any)
  const pages=import.meta.glob('../src/routes/\\(app\\)/**/+page.svelte')
  const routes=Object.entries(pages).map(([file,load])=>{
    const route=file.replace('../src/routes/(app)','').replace('/+page.svelte','')||'/'
    const names=[...route.matchAll(/\[([^\]]+)\]/g)].map(x=>x[1])
    return {route,names,match:new RegExp('^'+route.replace(/\[[^\]]+\]/g,'([^/]+)')+'/?$'),load}
  }).sort((a,b)=>a.names.length-b.names.length)
  let Component:any=null;let error='';let loadedPath='';let requested=''
  async function open(path:string){
    requested=path;error='';Component=null
    const route=routes.find(r=>r.match.test(path))
    if(!route){error='Choose a sample page from the navigation.';return}
    const match=path.match(route.match)!
    $page.params=Object.fromEntries(route.names.map((name,i)=>[name,match[i+1]]))
    try{const result:any=await route.load();if(requested===path){Component=result.default;loadedPath=path}}
    catch{error='This sample page could not be loaded.'}
  }
  $: if($page.url.pathname!==requested)void open($page.url.pathname)
  $: if($page.url.searchParams.has('settings'))showSettings.set($page.url.searchParams.get('settings')!)
</script>
<nav class="preview-pages" aria-label={$i18n.t("Sample pages")}><label><span class="preview-page-label">{$i18n.t("Browse sample pages")}</span> <select aria-label={$i18n.t("Browse sample pages")} value={$page.url.pathname} on:change={(event)=>goto(event.currentTarget.value)}>
  <option value="/">{$i18n.t("Welcome & chat")}</option><option value="/c/sample-chat">{$i18n.t("Sample conversation")}</option>
  <option value="/workspace/there">{$i18n.t("Knowledge, skills & research")}</option><option value="/workspace/models">{$i18n.t("Models")}</option><option value="/workspace/knowledge">{$i18n.t("Knowledge workspace")}</option><option value="/workspace/prompts">{$i18n.t("Prompts")}</option><option value="/workspace/skills">{$i18n.t("Skills")}</option><option value="/workspace/tools">{$i18n.t("Tools")}</option>
  <option value="/notes">{$i18n.t("Notes")}</option><option value="/calendar">{$i18n.t("Calendar")}</option><option value="/automations">{$i18n.t("Automations")}</option><option value="/playground/images">{$i18n.t("Image playground")}</option><option value="/playground/completions">{$i18n.t("Completions playground")}</option>
  <option value="/admin/users/overview">{$i18n.t("Administration \u00b7 users")}</option><option value="/admin/users/groups">{$i18n.t("Administration \u00b7 groups")}</option><option value="/admin/evaluations">{$i18n.t("Administration \u00b7 evaluations")}</option><option value="/admin/analytics">{$i18n.t("Administration \u00b7 analytics")}</option><option value="/admin/functions">{$i18n.t("Administration \u00b7 functions")}</option><option value="/admin/settings/general">{$i18n.t("Administration \u00b7 settings")}</option>
  <option value="/?settings=account">{$i18n.t("Account settings")}</option>
</select></label><StudioLanguagePicker /></nav>
<div data-preview-route={loadedPath} style="height:calc(100dvh - 86px)" class="flex w-full overflow-hidden bg-white text-gray-900 dark:bg-gray-900 dark:text-gray-100">
  <Sidebar />
  {#if error}<main class="p-10">{$i18n.t(error)}</main>
  {:else if Component}
    {#key loadedPath}
      {#if loadedPath.startsWith('/workspace')}<Workspace><svelte:component this={Component}/></Workspace>
      {:else if loadedPath.startsWith('/admin')}<Admin><svelte:component this={Component}/></Admin>
      {:else}<svelte:component this={Component}/>{/if}
    {/key}
  {:else}<p role="status" class="p-8">{$i18n.t("Loading sample page…")}</p>{/if}
</div>
<SettingsModal bind:show={$showSettings}/>
<style>
  .preview-pages{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:8px 16px;background:#0d1a38;color:#bcccf0;font:12px/1.6 Arial,sans-serif;border-bottom:1px solid #24375a}
  .preview-pages select{margin-left:14px;padding:4px 10px;border:1px solid #354d79;border-radius:5px;background:#132342;color:#f3f6ff;max-width:65vw}
  @media (max-width:480px){
    .preview-pages{gap:8px;padding-inline:8px}
    .preview-pages label{display:flex;align-items:center;flex:1 1 0;min-width:0}
    .preview-page-label{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0,0,0,0);clip-path:inset(50%);white-space:nowrap;border:0}
    .preview-pages select{flex:1 1 auto;width:100%;min-width:0;max-width:100%;margin-left:0}
  }
</style>
