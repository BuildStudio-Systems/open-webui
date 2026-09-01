<script lang="ts">
	import DOMPurify from 'dompurify';
	import { marked } from 'marked';

	import { toast } from 'svelte-sonner';

	import { onMount, getContext } from 'svelte';
	import { goto } from '$app/navigation';
	import { page } from '$app/stores';

	import { getBackendConfig } from '$lib/apis';
	import {
		ldapUserSignIn,
		getSessionUser,
		userSignIn,
		userSignUp,
		updateUserTimezone
	} from '$lib/apis/auths';

	import { WEBUI_API_BASE_URL, WEBUI_BASE_URL } from '$lib/constants';
	import { WEBUI_NAME, config, user, socket } from '$lib/stores';

	import { generateInitialsImage, canvasPixelTest, getUserTimezone } from '$lib/utils';

	import Spinner from '$lib/components/common/Spinner.svelte';
	import OnBoarding from '$lib/components/OnBoarding.svelte';
	import SensitiveInput from '$lib/components/common/SensitiveInput.svelte';
	import { redirect } from '@sveltejs/kit';

	const i18n = getContext('i18n');

	let loaded = false;

	let mode = $config?.features.enable_ldap ? 'ldap' : 'signin';

	let form = null;

	let name = '';
	let email = '';
	let password = '';
	let confirmPassword = '';

	let ldapUsername = '';

	let submitting = false;

	const setSessionUser = async (sessionUser, redirectPath: string | null = null) => {
		if (sessionUser) {
			console.log(sessionUser);
			toast.success(`You're now logged in.`);
			if (sessionUser.token) {
				localStorage.token = sessionUser.token;
			}
			$socket.emit('user-join', { auth: { token: sessionUser.token } });
			await user.set(sessionUser);
			await config.set(await getBackendConfig());

			// Update user timezone
			const timezone = getUserTimezone();
			if (sessionUser.token && timezone) {
				updateUserTimezone(sessionUser.token, timezone);
			}

			if (!redirectPath) {
				redirectPath = $page.url.searchParams.get('redirect') || '/';
			}

			goto(redirectPath);
			localStorage.removeItem('redirectPath');
		}
	};

	const signInHandler = async () => {
		const sessionUser = await userSignIn(email, password).catch((error) => {
			toast.error(`${error}`);
			return null;
		});

		await setSessionUser(sessionUser);
	};

	const signUpHandler = async () => {
		if ($config?.features?.enable_signup_password_confirmation) {
			if (password !== confirmPassword) {
				toast.error('Passwords do not match.');
				return;
			}
		}

		const sessionUser = await userSignUp(name, email, password, generateInitialsImage(name)).catch(
			(error) => {
				toast.error(`${error}`);
				return null;
			}
		);

		await setSessionUser(sessionUser);
	};

	const ldapSignInHandler = async () => {
		const sessionUser = await ldapUserSignIn(ldapUsername, password).catch((error) => {
			toast.error(`${error}`);
			return null;
		});
		await setSessionUser(sessionUser);
	};

	const submitHandler = async () => {
		if (submitting) {
			return;
		}

		submitting = true;
		try {
			if (mode === 'ldap') {
				await ldapSignInHandler();
			} else if (mode === 'signin') {
				await signInHandler();
			} else {
				await signUpHandler();
			}
		} finally {
			submitting = false;
		}
	};

	const oauthCallbackHandler = async () => {
		// Get the value of the 'token' cookie
		function getCookie(name) {
			const match = document.cookie.match(
				new RegExp('(?:^|; )' + name.replace(/([.$?*|{}()[\]\\/+^])/g, '\\$1') + '=([^;]*)')
			);
			return match ? decodeURIComponent(match[1]) : null;
		}

		const token = getCookie('token');
		if (!token) {
			return;
		}

		const sessionUser = await getSessionUser(token).catch((error) => {
			toast.error(`${error}`);
			return null;
		});

		if (!sessionUser) {
			return;
		}

		localStorage.token = token;
		await setSessionUser(sessionUser, localStorage.getItem('redirectPath') || null);
	};

	let onboarding = false;

	onMount(async () => {
		const redirectPath = $page.url.searchParams.get('redirect');
		const logout = $page.url.searchParams.get('state') === 'logout';

		if ($user && !logout) {
			goto(redirectPath || '/');
		} else {
			if (redirectPath) {
				localStorage.setItem('redirectPath', redirectPath);
			}
		}

		const error = $page.url.searchParams.get('error');
		if (error) {
			toast.error(error);
		}

		await oauthCallbackHandler();
		form = $page.url.searchParams.get('form');

		// Auto-redirect to SSO when OAUTH_AUTO_REDIRECT is enabled and the
		// deployment is unambiguously SSO-only (single provider, no login form,
		// no LDAP). Suppressed after logout, by ?form=, ?error=, onboarding,
		// trusted-header auth, or an existing session/token.
		if ($config?.oauth?.auto_redirect && !logout && !form && !error) {
			const providers = Object.keys($config?.oauth?.providers ?? {});
			if (
				providers.length === 1 &&
				$config?.features?.auth !== false &&
				$config?.features?.enable_login_form === false &&
				!$config?.features?.enable_ldap &&
				!$config?.features?.auth_trusted_header &&
				!$config?.onboarding &&
				!localStorage.token &&
				!document.cookie.split('; ').some((c) => c.startsWith('token='))
			) {
				window.location.href = `${WEBUI_BASE_URL}/oauth/${providers[0]}/login`;
				return;
			}
		}

		loaded = true;

		if (($config?.features?.auth_trusted_header ?? false) || $config?.features?.auth === false) {
			await signInHandler();
		} else {
			onboarding = $config?.onboarding ?? false;
		}
	});
</script>

<svelte:head>
	<!-- LICENSE covers this Open WebUI browser-title identifier.
	Do not alter, remove, obscure, or replace it except as LICENSE permits:
	https://docs.openwebui.com/license. -->
	<title>BuildStudio-There</title>
</svelte:head>

<OnBoarding
	bind:show={onboarding}
	getStartedHandler={() => {
		onboarding = false;
		mode = $config?.features.enable_ldap ? 'ldap' : 'signup';
	}}
/>

<div class="w-full h-screen max-h-[100dvh] text-white relative" id="auth-page">
	<div class="w-full h-full absolute top-0 left-0 bg-white dark:bg-black"></div>

	<div class="w-full absolute top-0 left-0 right-0 h-8 drag-region"></div>

	{#if loaded}
		<div
			class="fixed bg-transparent min-h-screen w-full flex justify-center z-50 text-black dark:text-white"
			id="auth-container"
		>
			<div class="w-full px-10 min-h-screen flex flex-col text-center">
				{#if ($config?.features.auth_trusted_header ?? false) || $config?.features.auth === false}
					<div class=" my-auto pb-10 w-full sm:max-w-md">
						<div
							class="flex items-center justify-center gap-3 text-xl sm:text-2xl text-center font-normal dark:text-gray-200"
						>
							<div>
								Signing in to {$WEBUI_NAME}
							</div>

							<div>
								<Spinner className="size-5" />
							</div>
						</div>
					</div>
				{:else}
					<div class="there-login-shell">
						<section class="there-brand-panel" aria-label="BuildStudio There system overview">
							<div class="there-brand-lockup">
								<img
									class="there-brand-mark"
									src="/assets/buildstudio-there-emblem.png"
									alt="BuildStudio There"
								/>
								<div class="there-brand-wordmark">
									<div class="there-brand-name"><strong>BuildStudio</strong><span>There</span></div>
									<div class="there-brand-rail" aria-hidden="true"></div>
									<div class="there-brand-system-name">ARTIFICIAL INTELLIGENCE SYSTEM</div>
								</div>
							</div>

							<div class="there-brand-copy">
								<p>PRIVATE AI INFRASTRUCTURE</p>
								<h1>I’m There, <span>BuildStudio’s AI agent.</span></h1>
								<div>
									I’m here to understand your needs, answer your questions, and help turn your ideas
									into action.
								</div>
							</div>

							<div class="there-capability-list" aria-label="System capabilities">
								<span>LOCAL MODELS</span>
								<span>AGENT ORCHESTRATION</span>
								<span>PRIVATE KNOWLEDGE</span>
							</div>
						</section>

						<section class="there-form-panel" aria-label="Sign in">
							<div class="there-form-panel__status">
								<span>SECURE ACCESS</span>
								<span><i></i> LOCAL SYSTEM</span>
							</div>

							<div id="auth-login-card" class="w-full dark:text-gray-100">
								{#if $config?.metadata?.auth_logo_position === 'center'}
									<div class="flex justify-center mb-6">
										<!-- LICENSE covers this Open WebUI sign-in logo.
									Do not alter, remove, obscure, or replace it except as LICENSE permits:
									https://docs.openwebui.com/license. -->
										<img
											id="logo"
											crossorigin="anonymous"
											src="{WEBUI_BASE_URL}/static/favicon.png?v=buildstudio-there-20260901"
											class="size-24 rounded-full"
											alt="{$WEBUI_NAME} logo"
										/>
									</div>
								{/if}
								<form
									class=" flex flex-col justify-center"
									on:submit={(e) => {
										e.preventDefault();
										submitHandler();
									}}
								>
									<div class="there-form-heading mb-1">
										<div>
											{#if $config?.onboarding ?? false}
												Get started with There
											{:else if mode === 'ldap'}
												Sign in with LDAP
											{:else if mode === 'signin'}
												Welcome back
											{:else}
												Create your account
											{/if}
										</div>
										<p>Sign in to continue to your BuildStudio AI workspace.</p>

										{#if $config?.onboarding ?? false}
											<div class="mt-1 text-xs font-normal text-gray-600 dark:text-gray-500">
												ⓘ {$WEBUI_NAME}
												does not make any external connections, and your data stays securely on your locally
												hosted server.
											</div>
										{/if}
									</div>

									{#if $config?.features.enable_login_form || $config?.features.enable_ldap || form}
										<div class="flex flex-col mt-4">
											{#if mode === 'signup'}
												<div class="mb-2">
													<label for="name" class="text-sm font-normal text-left mb-1 block"
														>Name</label
													>
													<input
														bind:value={name}
														type="text"
														id="name"
														class="my-0.5 w-full text-sm outline-hidden bg-transparent placeholder:text-gray-300 dark:placeholder:text-gray-600"
														autocomplete="name"
														placeholder="Enter your full name"
														required
													/>
												</div>
											{/if}

											{#if mode === 'ldap'}
												<div class="mb-2">
													<label for="username" class="text-sm font-normal text-left mb-1 block"
														>Username</label
													>
													<input
														bind:value={ldapUsername}
														type="text"
														class="my-0.5 w-full text-sm outline-hidden bg-transparent placeholder:text-gray-300 dark:placeholder:text-gray-600"
														autocomplete="username"
														name="username"
														id="username"
														placeholder="Enter your username"
														required
													/>
												</div>
											{:else}
												<div class="mb-2">
													<label for="email" class="text-sm font-normal text-left mb-1 block"
														>Email</label
													>
													<input
														bind:value={email}
														type="email"
														id="email"
														class="my-0.5 w-full text-sm outline-hidden bg-transparent placeholder:text-gray-300 dark:placeholder:text-gray-600"
														autocomplete="email"
														name="email"
														placeholder="Enter your email"
														required
													/>
												</div>
											{/if}

											<div>
												<label for="password" class="text-sm font-normal text-left mb-1 block"
													>Password</label
												>
												<SensitiveInput
													bind:value={password}
													type="password"
													id="password"
													outerClassName="there-sensitive-input flex flex-1"
													class="my-0.5 w-full text-sm outline-hidden bg-transparent placeholder:text-gray-300 dark:placeholder:text-gray-600"
													placeholder="Enter your password"
													showButtonLabel="Show or hide password"
													autocomplete={mode === 'signup' ? 'new-password' : 'current-password'}
													name="password"
													screenReader={true}
													required
													aria-required="true"
												/>
											</div>

											{#if mode === 'signup' && $config?.features?.enable_signup_password_confirmation}
												<div class="mt-2">
													<label
														for="confirm-password"
														class="text-sm font-normal text-left mb-1 block">Confirm password</label
													>
													<SensitiveInput
														bind:value={confirmPassword}
														type="password"
														id="confirm-password"
														outerClassName="there-sensitive-input flex flex-1"
														class="my-0.5 w-full text-sm outline-hidden bg-transparent"
														placeholder="Confirm your password"
														showButtonLabel="Show or hide password"
														autocomplete="new-password"
														name="confirm-password"
														required
													/>
												</div>
											{/if}
										</div>
									{/if}
									<div class="mt-5">
										{#if $config?.features.enable_login_form || $config?.features.enable_ldap || form}
											{#if mode === 'ldap'}
												<button
													class="bg-gray-700/5 hover:bg-gray-700/10 dark:bg-gray-100/5 dark:hover:bg-gray-100/10 dark:text-gray-300 dark:hover:text-white transition w-full rounded-full font-normal text-sm py-2.5 disabled:opacity-50 flex justify-center"
													type="submit"
													disabled={submitting}
												>
													<div class="self-center">Authenticate</div>

													{#if submitting}
														<div class="ml-1.5 self-center">
															<Spinner />
														</div>
													{/if}
												</button>
											{:else}
												<button
													class="bg-gray-700/5 hover:bg-gray-700/10 dark:bg-gray-100/5 dark:hover:bg-gray-100/10 dark:text-gray-300 dark:hover:text-white transition w-full rounded-full font-normal text-sm py-2.5 disabled:opacity-50 flex justify-center"
													type="submit"
													disabled={submitting}
												>
													<div class="self-center">
														{mode === 'signin'
															? 'Sign in'
															: ($config?.onboarding ?? false)
																? 'Create admin account'
																: 'Create account'}
													</div>

													{#if submitting}
														<div class="ml-1.5 self-center">
															<Spinner />
														</div>
													{/if}
												</button>

												{#if $config?.features.enable_signup && !($config?.onboarding ?? false)}
													<div class=" mt-4 text-sm text-center">
														{mode === 'signin'
															? "Don't have an account?"
															: 'Already have an account?'}

														<button
															class=" font-normal underline"
															type="button"
															on:click={() => {
																if (mode === 'signin') {
																	mode = 'signup';
																} else {
																	mode = 'signin';
																}
															}}
														>
															{mode === 'signin' ? 'Sign up' : 'Sign in'}
														</button>
													</div>
												{/if}
											{/if}
										{/if}
									</div>
								</form>

								{#if Object.keys($config?.oauth?.providers ?? {}).length > 0}
									<div class="inline-flex items-center justify-center w-full">
										<hr class="w-32 h-px my-4 border-0 dark:bg-gray-100/10 bg-gray-700/10" />
										{#if $config?.features.enable_login_form || $config?.features.enable_ldap || form}
											<span
												class="px-3 text-sm font-normal text-gray-900 dark:text-white bg-transparent"
												>or</span
											>
										{/if}

										<hr class="w-32 h-px my-4 border-0 dark:bg-gray-100/10 bg-gray-700/10" />
									</div>
									<div class="flex flex-col space-y-2">
										{#if $config?.oauth?.providers?.google}
											<button
												class="flex justify-center items-center bg-gray-700/5 hover:bg-gray-700/10 dark:bg-gray-100/5 dark:hover:bg-gray-100/10 dark:text-gray-300 dark:hover:text-white transition w-full rounded-full font-normal text-sm py-2.5"
												on:click={() => {
													window.location.href = `${WEBUI_BASE_URL}/oauth/google/login`;
												}}
											>
												<svg
													xmlns="http://www.w3.org/2000/svg"
													viewBox="0 0 48 48"
													class="size-6 mr-3"
													aria-hidden="true"
												>
													<path
														fill="#EA4335"
														d="M24 9.5c3.54 0 6.71 1.22 9.21 3.6l6.85-6.85C35.9 2.38 30.47 0 24 0 14.62 0 6.51 5.38 2.56 13.22l7.98 6.19C12.43 13.72 17.74 9.5 24 9.5z"
													/><path
														fill="#4285F4"
														d="M46.98 24.55c0-1.57-.15-3.09-.38-4.55H24v9.02h12.94c-.58 2.96-2.26 5.48-4.78 7.18l7.73 6c4.51-4.18 7.09-10.36 7.09-17.65z"
													/><path
														fill="#FBBC05"
														d="M10.53 28.59c-.48-1.45-.76-2.99-.76-4.59s.27-3.14.76-4.59l-7.98-6.19C.92 16.46 0 20.12 0 24c0 3.88.92 7.54 2.56 10.78l7.97-6.19z"
													/><path
														fill="#34A853"
														d="M24 48c6.48 0 11.93-2.13 15.89-5.81l-7.73-6c-2.15 1.45-4.92 2.3-8.16 2.3-6.26 0-11.57-4.22-13.47-9.91l-7.98 6.19C6.51 42.62 14.62 48 24 48z"
													/><path fill="none" d="M0 0h48v48H0z" />
												</svg>
												<span>Continue with Google</span>
											</button>
										{/if}
										{#if $config?.oauth?.providers?.microsoft}
											<button
												class="flex justify-center items-center bg-gray-700/5 hover:bg-gray-700/10 dark:bg-gray-100/5 dark:hover:bg-gray-100/10 dark:text-gray-300 dark:hover:text-white transition w-full rounded-full font-normal text-sm py-2.5"
												on:click={() => {
													window.location.href = `${WEBUI_BASE_URL}/oauth/microsoft/login`;
												}}
											>
												<svg
													xmlns="http://www.w3.org/2000/svg"
													viewBox="0 0 21 21"
													class="size-6 mr-3"
													aria-hidden="true"
												>
													<rect x="1" y="1" width="9" height="9" fill="#f25022" /><rect
														x="1"
														y="11"
														width="9"
														height="9"
														fill="#00a4ef"
													/><rect x="11" y="1" width="9" height="9" fill="#7fba00" /><rect
														x="11"
														y="11"
														width="9"
														height="9"
														fill="#ffb900"
													/>
												</svg>
												<span>Continue with Microsoft</span>
											</button>
										{/if}
										{#if $config?.oauth?.providers?.github}
											<button
												class="flex justify-center items-center bg-gray-700/5 hover:bg-gray-700/10 dark:bg-gray-100/5 dark:hover:bg-gray-100/10 dark:text-gray-300 dark:hover:text-white transition w-full rounded-full font-normal text-sm py-2.5"
												on:click={() => {
													window.location.href = `${WEBUI_BASE_URL}/oauth/github/login`;
												}}
											>
												<svg
													xmlns="http://www.w3.org/2000/svg"
													viewBox="0 0 24 24"
													class="size-6 mr-3"
													aria-hidden="true"
												>
													<path
														fill="currentColor"
														d="M12 0C5.37 0 0 5.37 0 12c0 5.31 3.435 9.795 8.205 11.385.6.105.825-.255.825-.57 0-.285-.015-1.23-.015-2.235-3.015.555-3.795-.735-4.035-1.41-.135-.345-.72-1.41-1.23-1.695-.42-.225-1.02-.78-.015-.795.945-.015 1.62.87 1.845 1.23 1.08 1.815 2.805 1.305 3.495.99.105-.78.42-1.305.765-1.605-2.67-.3-5.46-1.335-5.46-5.925 0-1.305.465-2.385 1.23-3.225-.12-.3-.54-1.53.12-3.18 0 0 1.005-.315 3.3 1.23.96-.27 1.98-.405 3-.405s2.04.135 3 .405c2.295-1.56 3.3-1.23 3.3-1.23.66 1.65.24 2.88.12 3.18.765.84 1.23 1.92 1.23 3.225 0 4.605-2.805 5.625-5.475 5.925.435.375.81 1.095.81 2.22 0 1.605-.015 2.895-.015 3.3 0 .315.225.69.825.57C20.565 21.795 24 17.31 24 12c0-6.63-5.37-12-12-12z"
													/>
												</svg>
												<span>Continue with GitHub</span>
											</button>
										{/if}
										{#if $config?.oauth?.providers?.oidc}
											<button
												class="flex justify-center items-center bg-gray-700/5 hover:bg-gray-700/10 dark:bg-gray-100/5 dark:hover:bg-gray-100/10 dark:text-gray-300 dark:hover:text-white transition w-full rounded-full font-normal text-sm py-2.5"
												on:click={() => {
													window.location.href = `${WEBUI_BASE_URL}/oauth/oidc/login`;
												}}
											>
												<svg
													xmlns="http://www.w3.org/2000/svg"
													fill="none"
													viewBox="0 0 24 24"
													stroke-width="1.5"
													stroke="currentColor"
													class="size-6 mr-3"
													aria-hidden="true"
												>
													<path
														stroke-linecap="round"
														stroke-linejoin="round"
														d="M15.75 5.25a3 3 0 0 1 3 3m3 0a6 6 0 0 1-7.029 5.912c-.563-.097-1.159.026-1.563.43L10.5 17.25H8.25v2.25H6v2.25H2.25v-2.818c0-.597.237-1.17.659-1.591l6.499-6.499c.404-.404.527-1 .43-1.563A6 6 0 1 1 21.75 8.25Z"
													/>
												</svg>

												<span>Continue with {$config?.oauth?.providers?.oidc ?? 'SSO'}</span>
											</button>
										{/if}
										{#if $config?.oauth?.providers?.feishu}
											<button
												class="flex justify-center items-center bg-gray-700/5 hover:bg-gray-700/10 dark:bg-gray-100/5 dark:hover:bg-gray-100/10 dark:text-gray-300 dark:hover:text-white transition w-full rounded-full font-normal text-sm py-2.5"
												on:click={() => {
													window.location.href = `${WEBUI_BASE_URL}/oauth/feishu/login`;
												}}
											>
												<span>Continue with Feishu</span>
											</button>
										{/if}
									</div>
								{/if}

								{#if $config?.features.enable_ldap && $config?.features.enable_login_form}
									<div class="mt-2">
										<button
											class="flex justify-center items-center text-xs w-full text-center underline"
											type="button"
											on:click={() => {
												if (mode === 'ldap')
													mode = ($config?.onboarding ?? false) ? 'signup' : 'signin';
												else mode = 'ldap';
											}}
										>
											<span>{mode === 'ldap' ? 'Continue with email' : 'Continue with LDAP'}</span>
										</button>
									</div>
								{/if}
							</div>
							{#if $config?.metadata?.login_footer}
								<div class="max-w-3xl mx-auto">
									<div class="mt-2 text-[0.7rem] text-gray-500 dark:text-gray-400 marked">
										{@html DOMPurify.sanitize(marked($config?.metadata?.login_footer))}
									</div>
								</div>
							{/if}
						</section>
					</div>
				{/if}
			</div>
		</div>

		{#if !$config?.metadata?.auth_logo_position}
			<div class="there-corner-mark fixed m-10 z-50">
				<div class="flex space-x-2">
					<div class=" self-center">
						<!-- LICENSE covers this Open WebUI sign-in logo.
						Do not alter, remove, obscure, or replace it except as LICENSE permits:
						https://docs.openwebui.com/license. -->
						<img
							id="logo"
							crossorigin="anonymous"
							src="{WEBUI_BASE_URL}/static/favicon.png?v=buildstudio-there-20260901"
							class=" w-6 rounded-full"
							alt=""
						/>
					</div>
				</div>
			</div>
		{/if}
	{/if}
</div>

<style>
	#auth-page {
		--there-bg: #071023;
		--there-panel: rgba(8, 18, 42, 0.86);
		--there-panel-deep: rgba(5, 13, 29, 0.82);
		--there-border: rgba(111, 148, 242, 0.24);
		--there-border-strong: rgba(111, 148, 242, 0.42);
		--there-text: #f4f7ff;
		--there-muted: #8293ba;
		--there-primary: #4c78ff;
		--there-primary-soft: #7da0ff;
		min-height: 100dvh;
		max-height: none;
		color: var(--there-text);
		background: var(--there-bg);
	}

	#auth-page > div:first-child {
		position: fixed;
		inset: 0;
		background:
			linear-gradient(rgba(99, 130, 209, 0.022) 1px, transparent 1px),
			linear-gradient(90deg, rgba(99, 130, 209, 0.022) 1px, transparent 1px),
			radial-gradient(ellipse 58% 86% at 9% 46%, rgba(40, 84, 204, 0.28), transparent 72%),
			radial-gradient(ellipse 42% 76% at 92% 52%, rgba(26, 102, 81, 0.12), transparent 76%),
			linear-gradient(118deg, #0c183a 0%, #09172f 42%, #071624 70%, #06130f 100%);
		background-size:
			36px 36px,
			36px 36px,
			auto,
			auto,
			auto;
	}

	#auth-container {
		padding: clamp(20px, 4vw, 54px);
		align-items: center;
		overflow-y: auto;
		color: var(--there-text);
	}

	#auth-container > div {
		width: 100%;
		min-height: auto;
		padding: 0;
	}

	.there-login-shell {
		position: relative;
		width: min(1160px, 100%);
		min-height: min(680px, calc(100dvh - 80px));
		margin: auto;
		display: grid;
		grid-template-columns: minmax(0, 1.12fr) minmax(390px, 0.78fr);
		overflow: hidden;
		border: 1px solid var(--there-border);
		border-radius: 24px;
		background: var(--there-panel);
		box-shadow:
			0 30px 90px rgba(0, 0, 0, 0.38),
			inset 0 1px 0 rgba(255, 255, 255, 0.035);
		backdrop-filter: blur(22px);
		text-align: left;
	}

	.there-login-shell::before {
		content: '';
		position: absolute;
		inset: 0 0 auto;
		height: 2px;
		z-index: 3;
		background: linear-gradient(90deg, #ef4052 0 12%, #4c78ff 40%, #20c1f0 74%, transparent);
	}

	.there-brand-panel {
		position: relative;
		padding: clamp(34px, 4vw, 54px);
		display: flex;
		flex-direction: column;
		justify-content: space-between;
		gap: 42px;
		overflow: hidden;
		background:
			radial-gradient(circle at 12% 14%, rgba(76, 120, 255, 0.18), transparent 38%),
			linear-gradient(145deg, rgba(18, 39, 88, 0.68), rgba(8, 20, 48, 0.36));
	}

	.there-brand-panel::after {
		content: '';
		position: absolute;
		width: 360px;
		height: 360px;
		right: -180px;
		bottom: -200px;
		border: 1px solid rgba(76, 120, 255, 0.12);
		border-radius: 50%;
		box-shadow:
			0 0 0 54px rgba(76, 120, 255, 0.025),
			0 0 0 108px rgba(76, 120, 255, 0.018);
		pointer-events: none;
	}

	.there-brand-lockup {
		position: relative;
		z-index: 1;
		width: min(100%, 590px);
		display: flex;
		align-items: center;
		gap: 18px;
		padding: 0;
		border: 0;
		background: transparent;
		box-shadow: none;
	}

	.there-brand-mark {
		width: 112px;
		height: 112px;
		flex: 0 0 auto;
		object-fit: contain;
		filter: drop-shadow(0 12px 24px rgba(2, 8, 23, 0.38));
	}

	.there-brand-wordmark {
		min-width: 0;
		flex: 1;
	}

	.there-brand-name {
		display: flex;
		align-items: baseline;
		gap: 12px;
		white-space: nowrap;
		line-height: 1;
	}

	.there-brand-name strong {
		color: #f4f7ff;
		font-size: clamp(32px, 3.3vw, 47px);
		font-weight: 760;
		letter-spacing: -0.055em;
	}

	.there-brand-name span {
		color: #5d86ff;
		font-size: clamp(27px, 2.8vw, 40px);
		font-weight: 720;
		letter-spacing: -0.04em;
	}

	.there-brand-rail {
		height: 3px;
		margin: 10px 0 9px;
		border-radius: 999px;
		background: linear-gradient(90deg, #ef4052 0 18%, #4c78ff 48%, #20c1f0);
	}

	.there-brand-system-name {
		color: #a7b6da;
		font:
			600 9px/1 'Cascadia Mono',
			Consolas,
			monospace;
		letter-spacing: 0.25em;
		text-align: center;
	}

	.there-brand-copy {
		position: relative;
		z-index: 1;
		max-width: 570px;
	}

	.there-brand-copy > p {
		margin: 0 0 17px;
		color: var(--there-primary-soft);
		font:
			650 10px/1.2 'Cascadia Mono',
			Consolas,
			monospace;
		letter-spacing: 0.17em;
	}

	.there-brand-copy h1 {
		margin: 0;
		color: var(--there-text);
		font-size: clamp(38px, 4.2vw, 58px);
		font-weight: 650;
		letter-spacing: -0.055em;
		line-height: 1.06;
	}

	.there-brand-copy h1 span {
		display: block;
		color: #93adff;
	}

	.there-brand-copy > div {
		max-width: 520px;
		margin-top: 21px;
		color: #93a5ca;
		font-size: 13px;
		line-height: 1.8;
	}

	.there-capability-list {
		position: relative;
		z-index: 1;
		display: flex;
		flex-wrap: wrap;
		gap: 8px;
	}

	.there-capability-list span {
		padding: 8px 10px;
		border: 1px solid rgba(111, 148, 242, 0.22);
		border-radius: 7px;
		color: #91a4cc;
		background: rgba(7, 16, 35, 0.34);
		font:
			600 9px/1 'Cascadia Mono',
			Consolas,
			monospace;
		letter-spacing: 0.09em;
	}

	.there-form-panel {
		position: relative;
		padding: clamp(42px, 4.4vw, 64px);
		display: flex;
		flex-direction: column;
		justify-content: center;
		border-left: 1px solid rgba(111, 148, 242, 0.14);
		background: var(--there-panel-deep);
	}

	.there-form-panel__status {
		position: absolute;
		top: 28px;
		left: clamp(42px, 4.4vw, 64px);
		right: clamp(42px, 4.4vw, 64px);
		display: flex;
		justify-content: space-between;
		color: #61759f;
		font:
			600 9px/1 'Cascadia Mono',
			Consolas,
			monospace;
		letter-spacing: 0.12em;
	}

	.there-form-panel__status span:last-child {
		display: flex;
		align-items: center;
		gap: 7px;
		color: #62d8b1;
	}

	.there-form-panel__status i {
		width: 6px;
		height: 6px;
		border-radius: 50%;
		background: currentColor;
		box-shadow: 0 0 12px currentColor;
	}

	#auth-login-card {
		width: 100%;
		max-width: 390px;
		margin: 26px auto 0;
		padding: 0;
		color: var(--there-text);
	}

	.there-form-heading > div:first-child {
		color: var(--there-text);
		font-size: 32px;
		font-weight: 650;
		letter-spacing: -0.045em;
	}

	.there-form-heading > p {
		margin: 9px 0 28px;
		color: var(--there-muted);
		font-size: 12px;
		line-height: 1.65;
	}

	#auth-login-card label {
		margin-bottom: 8px;
		color: #aab7d3;
		font-size: 11px;
	}

	#auth-login-card :global(input) {
		width: 100%;
		height: 50px;
		padding: 0 14px;
		border: 1px solid var(--there-border-strong);
		border-radius: 10px;
		outline: none;
		color: var(--there-text);
		background: rgba(11, 25, 55, 0.74);
		transition:
			border-color 0.18s,
			box-shadow 0.18s,
			background 0.18s;
	}

	#auth-login-card :global(input::placeholder) {
		color: #4f628d;
	}

	#auth-login-card :global(input:focus) {
		border-color: var(--there-primary);
		background: rgba(15, 31, 67, 0.9);
		box-shadow: 0 0 0 3px rgba(76, 120, 255, 0.1);
	}

	#auth-login-card :global(.there-sensitive-input) {
		height: 50px;
		padding-right: 13px;
		align-items: center;
		border: 1px solid var(--there-border-strong);
		border-radius: 10px;
		background: rgba(11, 25, 55, 0.74);
		transition:
			border-color 0.18s,
			box-shadow 0.18s,
			background 0.18s;
	}

	#auth-login-card :global(.there-sensitive-input:focus-within) {
		border-color: var(--there-primary);
		background: rgba(15, 31, 67, 0.9);
		box-shadow: 0 0 0 3px rgba(76, 120, 255, 0.1);
	}

	#auth-login-card :global(.there-sensitive-input input) {
		height: 100%;
		border: 0;
		background: transparent;
		box-shadow: none;
	}

	#auth-login-card :global(.there-sensitive-input button) {
		color: #8fa2c7;
	}

	#auth-login-card button[type='submit'] {
		height: 50px;
		align-items: center;
		border: 0;
		border-radius: 10px;
		color: #fff;
		background: linear-gradient(90deg, #446ff2, #4c78ff 56%, #5683ff);
		box-shadow: 0 12px 26px rgba(47, 85, 205, 0.25);
		font-weight: 700;
		transition:
			transform 0.18s,
			filter 0.18s;
	}

	#auth-login-card button[type='submit']:hover:not(:disabled) {
		filter: brightness(1.08);
		transform: translateY(-1px);
	}

	.there-corner-mark {
		display: none;
	}

	@media (max-width: 980px) {
		#auth-container {
			padding: 20px;
		}

		.there-login-shell {
			width: min(600px, 100%);
			min-height: auto;
			grid-template-columns: 1fr;
			overflow: visible;
		}

		.there-brand-panel {
			padding: 28px;
			gap: 26px;
		}

		.there-brand-lockup {
			width: min(100%, 520px);
			gap: 14px;
			padding: 0;
		}

		.there-brand-mark {
			width: 88px;
			height: 88px;
		}

		.there-brand-system-name {
			font-size: 8px;
			letter-spacing: 0.18em;
		}

		.there-brand-copy h1 {
			font-size: clamp(30px, 7vw, 42px);
		}

		.there-brand-copy > div,
		.there-capability-list {
			display: none;
		}

		.there-form-panel {
			padding: 64px 28px 38px;
			border-top: 1px solid rgba(111, 148, 242, 0.14);
			border-left: 0;
		}

		.there-form-panel__status {
			top: 25px;
			left: 28px;
			right: 28px;
		}

		#auth-login-card {
			margin-top: 0;
		}
	}

	@media (max-width: 520px) {
		#auth-container {
			padding: 12px;
		}

		.there-login-shell {
			border-radius: 18px;
		}

		.there-brand-panel {
			padding: 20px;
		}

		.there-brand-lockup {
			gap: 10px;
		}

		.there-brand-mark {
			width: 72px;
			height: 72px;
		}

		.there-brand-name {
			gap: 8px;
		}

		.there-brand-name strong {
			font-size: 26px;
		}

		.there-brand-name span {
			font-size: 23px;
		}

		.there-brand-system-name {
			font-size: 7px;
			letter-spacing: 0.11em;
		}

		.there-brand-copy > p {
			margin-bottom: 11px;
		}

		.there-brand-copy h1 {
			font-size: 29px;
		}

		.there-form-panel {
			padding: 60px 20px 30px;
		}

		.there-form-panel__status {
			left: 20px;
			right: 20px;
		}
	}
</style>
