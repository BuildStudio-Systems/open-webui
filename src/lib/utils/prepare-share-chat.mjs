// Import a selected PDF using the signed-in user's ordinary private file/chat APIs.
// Never submits a chat completion, executes a tool, or changes another draft.
export async function prepareShareChat({file,token,userId,current,request,storage,prompt,modelIds=[]}) {
  let fileId=null,chatId=null,creating=false;
  const uuid=/^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/;
  const check=()=>{if(!current())throw Error('context_changed');};
  async function identity() {
    check();const me=await request('/api/v1/auths/',{token});check();
    if(me.id!==userId||!['admin','user'].includes(me.role)||(me.role!=='admin'&&me.permissions?.chat?.file_upload===false))throw Error('identity_changed');
  }
  try {
    await identity();
    const data=new FormData();data.append('file',file);
    const saved=await request('/api/v1/files/?process=true',{token,method:'POST',body:data});
    if(!uuid.test(saved.id||''))throw Error('upload_unconfirmed');fileId=saved.id;check();
    const until=Date.now()+120000;let completed=false;
    while(Date.now()<until) {
      check();const state=await request('/api/v1/files/'+fileId+'/process/status',{token});check();
      if(state.status==='completed'){completed=true;break;}
      if(!['pending','processing'].includes(state.status))throw Error('pdf_processing_failed');
      await new Promise(resolve=>setTimeout(resolve,1000));
    }
    if(!completed)throw Error('pdf_processing_timeout');
    await identity();
    const attachment={type:'file',id:fileId,url:fileId,name:file.name,size:file.size,status:'uploaded',content_type:'application/pdf',collection_name:saved.meta?.collection_name||'file-'+fileId};
    // A new, separately owned chat holds the file even if the browser draft is lost.
    creating=true;
    const chat=await request('/api/v1/chats/new',{token,method:'POST',body:JSON.stringify({chat:{title:file.name,models:modelIds,history:{messages:{},currentId:null},messages:[],files:[attachment],tags:[],timestamp:Date.now()},folder_id:null})});
    if(!uuid.test(chat.id||''))throw Error('chat_unconfirmed');chatId=chat.id;check();
    storage.setItem('chat-input-'+chatId,JSON.stringify({prompt,files:[attachment],selectedToolIds:[],selectedSkillIds:[],selectedFilterIds:[],webSearchEnabled:false,imageGenerationEnabled:false,codeInterpreterEnabled:false}));
    return {chatId,fileId};
  } catch(error) {
    // Once chat creation begins its outcome can be uncertain. Keep its private
    // file instead of deleting a file that may already belong to that chat.
    if(fileId&&!creating)await request('/api/v1/files/'+fileId,{token,method:'DELETE'}).catch(()=>{});
    throw error;
  }
}
