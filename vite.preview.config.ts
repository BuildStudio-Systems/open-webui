import {defineConfig} from 'vite'
import {svelte, vitePreprocess} from '@sveltejs/vite-plugin-svelte'
import path from 'node:path'
import {fileURLToPath} from 'node:url'
const root=path.dirname(fileURLToPath(import.meta.url))
export default defineConfig({
  root:path.join(root,'preview'),base:'/showcase/there/',publicDir:false,
  plugins:[{name:'preview-native-adapters',enforce:'pre',transform(code,id){
    if(id.includes('/src/')&&/\.(svelte|ts|css)$/.test(id))return code
      .replaceAll('localStorage.token', "'sample-display-only'")
      .replace(/`\$\{WEBUI_API_BASE_URL\}\/(?:users|models)\/[^`]*\/profile\/image[^`]*`/g, "'/showcase/there/user.png'")
      .replace(/"\{WEBUI_API_BASE_URL\}\/(?:users|models)\/[^\"]*\/profile\/image[^\"]*"/g, '"/showcase/there/user.png"')
      .replaceAll('${WEBUI_BASE_URL}/static/', '/showcase/there/static/')
      .replaceAll('{WEBUI_BASE_URL}/static/', '/showcase/there/static/')
      .replace(/(["'`(])\/(assets\/|static\/|user\.png|favicon\.png|image-placeholder\.png)/g,'$1/showcase/there/$2')
  }},svelte({configFile:false,preprocess:vitePreprocess(),onwarn(warning,handler){if(warning.code?.startsWith('a11y')||warning.code==='css_unused_selector')return;handler(warning)}})],
  resolve:{alias:{'$lib':path.join(root,'src/lib'),'$app/navigation':path.join(root,'preview/navigation.ts'),'$app/stores':path.join(root,'preview/navigation.ts'),'$app/environment':path.join(root,'preview/environment.ts')}},
  define:{APP_VERSION:JSON.stringify('0.11.3 · Public preview'),APP_BUILD_HASH:JSON.stringify('fictional-display')},
  build:{outDir:path.join(root,'preview-dist'),emptyOutDir:true,sourcemap:false},
  worker:{format:'es'},
})
