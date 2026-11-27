<script lang="ts">
  let { domain }: { domain: { label: string; color: string } | null } = $props();
</script>
{#if domain}<span class="pool-source-badge" style="background:{domain.color}">{domain.label}</span>{/if}
<style>
  .pool-source-badge { position: absolute; top: 5px; right: 5px; font-size: .6rem; font-weight: bold; padding: 1px 5px; border-radius: 3px; pointer-events: none; line-height: 1.4; text-shadow: none; opacity: .92; color: #fff; }
</style>
