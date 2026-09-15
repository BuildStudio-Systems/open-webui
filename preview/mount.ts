import {mount} from 'svelte'
import App from './App.svelte'
import '../src/tailwind.css'
import '../src/app.css'
document.documentElement.classList.add('dark')
mount(App,{target:document.getElementById('app')!})
