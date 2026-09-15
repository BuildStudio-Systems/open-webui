<script lang="ts">
  import i18n, { changeLanguage } from '$lib/i18n'
  import { languageFlags } from '$lib/i18n/flags'
  let open = false
  const languages = [{code:'en-US',flag:'en',label:'English'},{code:'ja-JP',flag:'ja',label:'日本語'},{code:'zh-CN',flag:'zh',label:'中文'}] as const
  $: current = languages.find(l => l.code === $i18n.language) ?? languages[0]
  function choose(code: string) { changeLanguage(code); open = false }
</script>
<details class="studio-language-picker" bind:open>
  <summary aria-label={$i18n.t('Language')}><img src={languageFlags[current.flag]} alt=""/><span>{current.label}</span><span aria-hidden="true">⌄</span></summary>
  <div class="studio-language-options" role="group" aria-label={$i18n.t('Language')}>
    {#each languages as language}<button type="button" lang={language.code} aria-pressed={current.code === language.code} on:click={() => choose(language.code)}><img src={languageFlags[language.flag]} alt=""/>{language.label}{#if current.code === language.code}<span aria-hidden="true">✓</span>{/if}</button>{/each}
  </div>
</details>
<style>
.studio-language-picker{position:relative;flex-shrink:0;font-size:13px}.studio-language-picker summary{display:flex;align-items:center;gap:8px;list-style:none;cursor:pointer;border:1px solid #63718a66;border-radius:7px;padding:6px 10px;min-height:32px}.studio-language-picker summary::-webkit-details-marker{display:none}.studio-language-picker img{width:21px;height:14px;border-radius:2px}.studio-language-options{position:absolute;right:0;top:calc(100% + 6px);min-width:150px;background:#15213a;color:#f1f5fc;border:1px solid #3a4c69;border-radius:8px;padding:5px;z-index:100;box-shadow:0 12px 30px #0005}.studio-language-options button{display:flex;align-items:center;gap:10px;width:100%;padding:9px;text-align:left;border-radius:4px}.studio-language-options button:hover,.studio-language-options button:focus-visible{background:#294161}.studio-language-options button span{margin-left:auto}
:global(html[lang="en-US"] body){font-family:Inter,Arial,sans-serif}
:global(html[lang="ja-JP"] body){font-family:"Noto Sans JP","Yu Gothic UI","Hiragino Kaku Gothic ProN",sans-serif}
:global(html[lang="zh-CN"] body){font-family:"Noto Sans SC","Microsoft YaHei","PingFang SC",sans-serif}
</style>
