import { mount } from 'svelte';
import Harness from './Harness.svelte';
import '../../../src/tailwind.css';
import '../../../src/app.css';
mount(Harness, { target: document.getElementById('app')! });
