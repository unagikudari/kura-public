<script lang="ts">
  import { enhance } from '$app/forms';
  import { page } from '$app/state';
  let { form } = $props();
  let submitting = $state(false);
  import { t } from '#lib/i18n/index.ts';
  const m = t();
</script>

<svelte:head><title>{m.login.title}</title></svelte:head>

<main class="login-wrap">
  <div class="login-panel">
    <div class="brand"><span class="brand-mark">蔵</span><span>kura</span></div>
    <h1>{m.login.welcome}</h1>
    <p class="lead">{m.login.lead}</p>
    <form method="POST" use:enhance={() => {
      submitting = true;
      return async ({ update }) => { try { await update(); } finally { submitting = false; } };
    }} autocomplete="on" aria-busy={submitting}>
      {#if form?.error}<div class="err" role="alert">{form.error}</div>{/if}
      <label for="email">{m.login.email}</label>
      <input id="email" type="email" name="email" placeholder={m.login.emailPlaceholder}
        autocomplete="username" value={form?.email ?? ''} required />
      <label for="account-key">{m.login.key}</label>
      <input id="account-key" type="password" name="key" placeholder={m.login.keyPlaceholder}
        autocomplete="current-password" required aria-describedby="key-help" />
      <p class="field-hint" id="key-help">{m.login.keyHelp}</p>
      <button type="submit" disabled={submitting}>{submitting ? m.login.signingIn : m.login.signIn}</button>
    </form>
    <div class="recovery">
      <p>{m.login.askAdmin}</p>
      {#if page.data.sourceUrl}<p><a href={page.data.sourceUrl} target="_blank" rel="noopener noreferrer">{m.common.sourceCode}</a></p>{/if}
    </div>
  </div>
</main>

<style>
  .login-wrap { height: 100vh; height: 100dvh; overflow: auto; box-sizing: border-box; display: flex; flex-direction: column; align-items: center; padding: 32px 20px; background: var(--background); }
  .login-panel { flex-shrink: 0; margin-block: auto; box-sizing: border-box; width: 100%; max-width: 420px; padding: 12px; border: 1px solid #373f45; border-radius: var(--control-radius); background: var(--surface); }
  .brand { display: flex; align-items: center; gap: 10px; color: #f0e9db; font-size: 22px; font-weight: 650; margin-bottom: 28px; }
  .brand-mark { color: var(--accent); font-size: 32px; line-height: 1; }
  h1 { margin: 0 0 10px; font-size: 23px; font-weight: 650; color: #f0ece5; letter-spacing: .02em; }
  .lead { margin: 0 0 8px; color: #b9c2c9; font-size: 13px; line-height: 1.8; }
  form { display: flex; flex-direction: column; }
  label { margin: 0 0 8px; color: #dbe0e4; font-size: 13px; }
  input { box-sizing: border-box; width: 100%; min-width: 0; min-height: var(--control-height); padding: 0 var(--control-padding-x); border-radius: var(--control-radius); border: 1px solid #46515a; background: #101518; color: #ece9e2; font-size: 16px; margin-bottom: 8px; }
  input::placeholder { color: #9ba7b1; font-size: 14px; }
  input:focus { border-color: #d9bd87; }
  #account-key { margin-bottom: 8px; }
  .field-hint { margin: 0 0 8px; color: #abb6bf; font-size: 12px; line-height: 1.7; }
  button { min-height: var(--control-height); padding: 0 var(--control-padding-x); border-radius: var(--control-radius); border: 1px solid #d9bd87; background: #d9bd87; color: #21201d; font-size: var(--control-font); font-weight: 650; cursor: pointer;  box-sizing: border-box; }
  button:hover { background: #ead0a0; }
  button:disabled { opacity: .65; cursor: wait; }
  .err { margin-bottom: 20px; padding: 12px; border: 1px solid #a65e5e; border-radius: var(--control-radius); background: #3d2424; color: #ffd0cb; font-size: 13px; line-height: 1.6; overflow-wrap: anywhere; }
  .recovery { margin-top: 10px; padding-top: 8px; border-top: 1px solid #373f45; font-size: 12px; line-height: 1.8; }
  .recovery a { color: var(--accent); text-underline-offset: 3px; }
  .recovery p { color: #aeb8c0; margin: 10px 0 0; }
  @media (max-width: 480px) { .login-wrap { padding: 12px 8px; } .login-panel { padding: 12px; } }

  .login-wrap { background: radial-gradient(ellipse at 50% 0%, #e2c58c0c, transparent 65%), var(--background);  padding: 12px 8px; }
  .login-panel { box-shadow: var(--shadow); border-top-color: #e2c58c66;  padding: 12px; }

</style>
