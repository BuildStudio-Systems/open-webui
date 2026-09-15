const time=1789459200
const avatar='/showcase/there/user.png'
export const sampleUser={id:'sample-user',name:'Alex Morgan · Sample',email:'alex@example.invalid',role:'admin',profile_image_url:avatar,created_at:time,updated_at:time,permissions:{},settings:{},info:{}}
export const sampleModels=[{id:'sample-assistant',name:'Studio Assistant · Sample',object:'model',owned_by:'openai',info:{id:'sample-assistant',name:'Studio Assistant · Sample',base_model_id:null,user_id:sampleUser.id,meta:{description:'Fictional AI assistant for this read-only display.',profile_image_url:avatar,capabilities:{vision:true,file_upload:true}},params:{},access_grants:[],is_active:true,created_at:time,updated_at:time}}]
export const sampleConfig={name:'BuildStudio There',version:'Public preview',default_models:'sample-assistant',default_locale:'en-US',default_prompt_suggestions:[],features:{auth:true,enable_signup:false,enable_websocket:false,enable_community_sharing:false,enable_plugins:true,enable_notes:true,enable_channels:true,enable_automations:true,enable_calendar:true,enable_image_generation:true,enable_web_search:true,enable_admin_export:false,enable_admin_chat_access:true},oauth:{providers:{}},file:{max_size:20,max_count:5},ui:{},audio:{tts:{engine:''},stt:{engine:''}}}
const paged=(items:any[])=>({items,total:items.length,has_more:false})
const base={user_id:sampleUser.id,created_at:time,updated_at:time,access_grants:[],user:sampleUser,meta:{tags:[]}}
const knowledge={...base,id:'sample-knowledge',name:'Studio Handbook · Sample',description:'Fictional product and engineering notes.',data:{file_ids:[]},files:[],base_type:'document'}
const note={...base,id:'sample-note',title:'Product research · Sample',data:{content:{md:'# Sample research\n\nA fictional note illustrating collaborative research and product planning.'}},updated_at:time*1e9,created_at:time*1e9}
const prompt={...base,command:'sample-review',name:'Sample review',title:'Review a product idea',content:'Summarize a fictional idea, its audience and its constraints.'}
const skill={...base,id:'sample-skill',name:'Research brief · Sample',description:'Structure a fictional research brief.',content:'# Research brief\n\nSummarize context, observations and open questions.'}
const chat={...base,id:'sample-chat',title:'Product planning · Sample',chat:{title:'Product planning · Sample',models:['sample-assistant'],messages:[{id:'q1',role:'user',content:'How can we organize the studio research?',timestamp:time},{id:'a1',role:'assistant',content:'## A sample research workflow\n\n1. Collect observations in the knowledge workspace.\n2. Compare ideas and record decisions.\n3. Keep reusable prompts and skills together.\n\nThis is a fictional conversation. No AI is connected.',model:'sample-assistant',done:true,timestamp:time}],history:{currentId:'a1',messages:{q1:{id:'q1',parentId:null,childrenIds:['a1'],role:'user',content:'How can we organize the studio research?',timestamp:time},a1:{id:'a1',parentId:'q1',childrenIds:[],role:'assistant',content:'## A sample research workflow\n\nCollect knowledge, compare ideas and record decisions. This is a fictional conversation.',model:'sample-assistant',done:true,timestamp:time}}}},pinned:false,archived:false,folder_id:null}
const paper={id:'sample-paper',title:'Human-centered knowledge work · Fictional study',authors:['Sample Research Group'],year:2026,source:'Sample library',abstract:'A fictional paper illustrating the literature research interface.',url:'https://paper.example.invalid/'}
const automation={...base,id:'sample-automation',folder_id:null,name:'Weekly research digest · Sample',data:{prompt:'Summarize the fictional studio research notes.',model_id:'sample-assistant',rrule:'FREQ=WEEKLY;BYDAY=MO;BYHOUR=9'},is_active:false,last_run_at:time,next_run_at:null,last_run:{id:'sample-run',automation_id:'sample-automation',chat_id:'sample-chat',status:'completed',error:null,created_at:time},next_runs:[]}
export function fixture(raw:string,method:string){
  const u=new URL(raw,'https://preview.invalid');const p=u.pathname
  if(method!=='GET')return undefined
  if(p==='/api/config')return sampleConfig
  if(p==='/api/v1/users/default/permissions')return {workspace:{models:true,knowledge:true,prompts:true,tools:true,skills:true},chat:{},features:{}}
  if(p==='/api/v1/groups/'||p==='/api/v1/groups/all')return [{...base,id:'sample-group',name:'Studio Research · Sample',description:'Fictional research team.',member_count:2,permissions:{},data:{}}]
  if(p==='/api/events'||p==='/api/events/webhooks')return []
  if(p==='/api/v1/auths/admin/config/ldap/server')return {enable_ldap:false,host:'',port:636,attribute_for_mail:'mail',attribute_for_username:'uid'}
  if(p==='/api/v1/auths/admin/config/oauth')return {providers:{}}
  if(p==='/api/v1/pipelines/list')return {data:[]}
  if(p==='/api/v1/retrieval/embedding')return {RAG_EMBEDDING_ENGINE:'',RAG_EMBEDDING_MODEL:'Sample embedding model',RAG_EMBEDDING_BATCH_SIZE:1,openai_config:{},ollama_config:{},azure_openai_config:{}}
  if(p==='/api/v1/retrieval/config')return {CONTENT_EXTRACTION_ENGINE:'',CHUNK_SIZE:1000,CHUNK_OVERLAP:100,ALLOWED_FILE_EXTENSIONS:['pdf','md','txt'],web:{ENABLE_WEB_SEARCH:true,WEB_SEARCH_ENGINE:'duckduckgo',WEB_SEARCH_RESULT_COUNT:3,WEB_SEARCH_DOMAIN_FILTER_LIST:[],YOUTUBE_LOADER_LANGUAGE:[]}}
  if(p==='/api/v1/images/config')return {ENABLE_IMAGE_GENERATION:true,ENABLE_IMAGE_EDIT:true,IMAGE_GENERATION_ENGINE:'openai',IMAGE_GENERATION_MODEL:'Sample image model',IMAGE_SIZE:'1024x1024',IMAGE_STEPS:20,IMAGE_EDIT_ENGINE:'openai',IMAGE_EDIT_MODEL:'Sample edit model',IMAGE_EDIT_SIZE:'1024x1024',COMFYUI_WORKFLOW_NODES:[],IMAGES_EDIT_COMFYUI_WORKFLOW_NODES:[]}
  if(p==='/api/models'||p==='/api/models/base')return {data:sampleModels}
  if(p==='/api/version'||p.includes('/version/'))return {version:'Public preview',current:'Public preview',latest:'Public preview'}
  if(p==='/api/v1/auths/'||p==='/api/v1/users/sample-user')return sampleUser
  if(p.includes('/users/')&&p.endsWith('/settings'))return {ui:{models:['sample-assistant']},keybindings:{}}
  if(p.includes('/users/')&&(p.includes('/list')||p.endsWith('/all')||p.endsWith('/users/')))return {users:[sampleUser,{...sampleUser,id:'sample-member',name:'Taylor Lee · Sample',email:'taylor@example.invalid',role:'user'}],total:2}
  if(p.includes('/there/')){
    const tail=p.split('/there')[1]
    if(tail==='/status')return {version:'Public preview',modules:['Knowledge','Skills','Research','Personal history'].map(name=>({id:name,name,state:'ready',detail:'Fictional display; execution disabled.'}))}
    if(tail==='/knowledge')return paged([knowledge,{...knowledge,id:'sample-faq',name:'Common questions · Sample',base_type:'faq'}])
    if(tail.endsWith('/documents'))return paged([{id:'sample-document',title:'Studio Handbook',file_name:'Sample-handbook.md',parse_status:'completed',created_at:time}])
    if(tail.endsWith('/chunks'))return paged([{id:'sample-chunk',content:'Fictional knowledge excerpt for interface demonstration.',content_revision:1,chunk_index:0,chunk_type:'text',index_status:'indexed'}])
    if(tail.endsWith('/revisions'))return {items:[{revision:1,content:'Fictional knowledge excerpt.',edited_at:'2026-09-15T08:00:00Z'}],current_revision:1}
    if(tail.endsWith('/faq'))return paged([{id:'sample-faq-entry',standard_question:'What does the studio research?',answers:['Software, AI-assisted workflows and product design.'],similar_questions:[]}])
    if(tail.includes('/faq/'))return {data:{id:'sample-faq-entry',standard_question:'What does the studio research?',answers:['Software, AI-assisted workflows and product design.'],similar_questions:[]}}
    if(tail==='/catalog')return paged([skill])
    if(tail.startsWith('/catalog/'))return {...skill,digest:'sample-display',version:'1.0'}
    if(tail==='/papers'||tail==='/research')return {...paged([paper]),sources:[{source:'Sample library',status:'ok',returned:1}],partial:false}
    if(tail.includes('/history')||tail.includes('/personal-history'))return {items:[{chat_id:'sample-chat',message_id:'a1',title:chat.title,question:'How can we organize research?',answer:'Collect knowledge and record decisions. Fictional sample only.',truncated:false}],page:1,has_more:false,scope:'sample'}
    if(tail==='/operations')return paged([{id:'sample-operation',action:'sample_import',state:'completed',created_at:time}])
    if(tail.includes('/wiki/'))return tail.includes('/page?')?{data:{slug:'sample',title:'Sample wiki',content:'Fictional knowledge page',version:1}}:paged([{slug:'sample',title:'Sample wiki',content:'Fictional knowledge page',version:1}])
  }
  if(p==='/api/v1/chats/sample-chat')return chat
  if(p==='/api/v1/chats/'||p.includes('/chats/list')||p.includes('/chats/search'))return Number(u.searchParams.get('page')||1)>1?[]:[chat]
  if(p.includes('/chats/')&&(p.includes('/pinned')||p.includes('/tags')||p.includes('/archived')))return []
  if(p.includes('/models/')){
    if(p.endsWith('/list'))return paged(sampleModels.map(m=>({...m.info,user:sampleUser})))
    if(p.endsWith('/tags'))return []
    if(p.includes('/model'))return sampleModels[0].info
    return sampleModels.map(m=>m.info)
  }
  if(p.includes('/knowledge/'))return p.endsWith('/search')||p.endsWith('/list')?paged([knowledge]):p.endsWith('/sample-knowledge')?knowledge:[knowledge]
  if(p.includes('/notes/'))return p.endsWith('/sample-note')?note:p.endsWith('/search')?paged([note]):p.endsWith('/pinned')?[]:[note]
  if(p.includes('/prompts/'))return p.endsWith('/list')?paged([prompt]):[prompt]
  if(p.includes('/skills/'))return p.endsWith('/list')?paged([skill]):p.endsWith('/id/sample-skill')?skill:[skill]
  if(p.includes('/tools/')||p.includes('/functions/'))return []
  if(p.includes('/analytics/'))return {total_users:2,total_chats:1,total_models:1,total_messages:2,total_input_tokens:320,total_output_tokens:180,total_tokens:500,users:[{user_id:sampleUser.id,name:sampleUser.name,email:sampleUser.email,count:2,input_tokens:320,output_tokens:180,total_tokens:500}],models:[{model_id:'sample-assistant',count:2,unique_users:1,unique_chats:1,input_tokens:320,output_tokens:180,total_tokens:500}],data:[{date:'2026-09-15',models:{'sample-assistant':2}}],items:[],total:0}
  if(p==='/api/v1/calendars/')return [{...base,id:'sample-calendar',name:'Studio calendar · Sample',color:'#4c78ff',is_default:true,is_system:false,data:{}}]
  if(p==='/api/v1/calendars/events')return [{...base,id:'sample-event',calendar_id:'sample-calendar',title:'Design review · Sample',description:'Fictional studio planning session.',start_at:time,end_at:time+3600,all_day:false,rrule:null,color:'#4c78ff',location:'Sample meeting room',data:{},is_cancelled:false,attendees:[]}]
  if(p==='/api/v1/automations/list')return paged([{...automation,created_at:time*1e9,updated_at:time*1e9}])
  if(p==='/api/v1/automations/sample-automation')return automation
  if(p==='/api/v1/automations/sample-automation/runs')return paged([automation.last_run])
  if(p.endsWith('/config')||p.endsWith('/settings')||p.includes('/configs/'))return {}
  if(/^\/api\/v1\/(tools|functions|folders|channels|groups|tasks|automations|calendar|terminals|tags|evaluations|feedbacks|memories)/.test(p))return /list|search/.test(p)?paged([]):[]
  return undefined
}
