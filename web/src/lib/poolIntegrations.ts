import type { Component } from 'svelte';
// Acquisition remains optional; the library renders without source adapters.
const adapters = import.meta.glob('/src/lib/poolIntegrations/*.svelte', {eager:true}) as
  Record<string, {default: Component<{poolId:number}>}>;
export const PoolAcquisition = Object.values(adapters)[0]?.default;
